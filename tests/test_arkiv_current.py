"""Current-state resolution and clean-instance recovery use the existing task lifecycle."""

import json
import shutil
import time
from types import SimpleNamespace

import httpx
import pytest

from agent import ccip_simulation
from agent.arkiv import ArkivError, ArkivStore
from agent.ccip import register_ccip_tools
from agent.config import Config
from agent.engine import Engine
from agent.server import create_app
from agent.tools import Context, Registry, register_builtins
from agent.wallet import Wallets
from tests.test_agent import call
from tests.test_ccip import DEST_HASH, MESSAGE, RECIPIENT, SENDER, CCIPChain
from tests.test_web3 import MemoryArkiv, approve
from tests.test_web3 import config as config


def test_terminal_checkpoint_wins_before_status_filter_and_stale_revision(config):
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    try:
        store.recover()
        task = store.create_task("u", "Research", "Read sources", "Evidence")
        store.update_task("u", task["id"], status="completed")
        # A delayed/replayed old worker checkpoint must not reopen this task, even with a later revision.
        stale = dict(bridge.rows[0])
        old = store.vault.open(stale["encrypted"], f"{bridge.instance}:{task['id']}:{stale['revision']}")
        stale["revision"] = str(int(bridge.rows[-1]["revision"]) + 100)
        stale["encrypted"] = store.vault.seal(old, f"{bridge.instance}:{task['id']}:{stale['revision']}")
        bridge.rows.append(stale)
        assert store.tasks("u", status="queued") == []
        assert store.progress("u", task["id"])["task"]["status"] == "completed"
        store.recover()
        assert store.claim() is None
        with pytest.raises(ValueError, match="cannot be reopened"):
            store.update_task("u", task["id"], status="queued")
    finally:
        store.close()


def test_public_metadata_is_bound_to_ciphertext(config):
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    try:
        store.recover()
        task = store.create_task("u", "Private title", "Private instruction", "Private condition")
        bridge.rows[-1]["public_state"] = {**bridge.rows[-1]["public_state"], "protocol": "attacker"}
        with pytest.raises(ArkivError, match="differs"):
            store.progress("u", task["id"])
    finally:
        store.close()


async def test_http_task_list_and_progress_read_arkiv_instead_of_a_stale_cache(config):
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    task = store.create_task("u", "Persisted work", "Work", "Evidence")
    store.update_task("u", task["id"], status="completed")
    with store.db:
        store.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
    app = create_app(config)
    app.state.runtime = SimpleNamespace(store=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/tasks", params={"user_id": "u", "status": "running"})).json() == []
            result = await client.get(f"/tasks/{task['id']}", params={"user_id": "u"})
            assert result.json()["task"]["status"] == "completed"
            assert result.json()["public_state"]["protocol"] == "ira"
            assert (await client.get(f"/tasks/{task['id']}", params={"user_id": "other"})).status_code == 404
            bridge.fail = True
            assert (await client.get("/tasks", params={"user_id": "u"})).status_code == 503
    finally:
        store.close()


async def test_clean_instance_recovers_submitted_ccip_and_finishes_without_another_payment(config, tmp_path):
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    config.env["AGENT_CCIP_SENDER_ADDRESS"] = SENDER
    chain = CCIPChain()
    chain.allowance = 10**18
    wallets = Wallets(config, store, chain)
    wallets.ccip.destination_rpc_override = chain.destination
    wallets.create("u")
    task = store.create_task("u", "Existing cross-chain payment", "Send once and track delivery", "Finalized delivery")
    payment = await wallets.ccip.prepare("u", task["id"], RECIPIENT, "0.001")
    payment = await approve(wallets, store, payment)
    chain.confirm(payment)
    payment = await wallets.status("u", payment["id"])
    assert payment["status"] == "in_transit" and payment["message_id"] == MESSAGE
    source_sends = sum(method == "eth_sendRawTransaction" for method, _ in chain.calls)
    assert source_sends == 1
    await wallets.close()
    store.close()

    # New directory, empty SQLite, no payment journal and no wallet signing key.
    clean_root = tmp_path / "clean-ira"
    clean_root.mkdir()
    shutil.copyfile(config.root / "inference.toml", clean_root / "inference.toml")
    clean = Config(clean_root, environ={"AGENT_TASK_BACKEND": "arkiv"})
    clean.data_dir.mkdir()
    shutil.copyfile(config.data_dir / "vault.key", clean.data_dir / "vault.key")
    recovered = ArkivStore(clean.db_path, clean, bridge)
    recovered.recover()
    calls_after_restart = []

    async def observing_rpc(method, params):
        calls_after_restart.append(method)
        if method.startswith(("eth_send", "wallet_")):
            raise AssertionError("Recovery cannot submit payments")
        return await chain(method, params)

    observer = Wallets(clean, recovered, observing_rpc)
    observer.ccip.destination_rpc_override = chain.destination
    try:
        assert list(clean.data_dir.glob("wallet-*.json")) == []
        assert list(clean.data_dir.glob("payment-*.json")) == []
        assert recovered.task("u", task["id"])["status"] == "needs_input"
        state = recovered.progress("u", task["id"])["public_state"]
        assert state["protocol"] == "ccip" and state["order_id"] == MESSAGE
        assert state["source_tx"] == payment["source_tx_hash"]
        chain.deliver(payment)
        assert (await observer.status("u", payment["id"]))["status"] == "delivered"
        # A second crash after delivery but before finish_task must still permit receipt-based completion.
        recovered.recover()
        assert recovered.task("u", task["id"])["status"] == "queued"

        registry = Registry()
        register_builtins(registry)
        register_ccip_tools(registry)
        engine = Engine(clean, recovered, None, registry)
        engine.wallets = observer
        ctx = Context(engine, "u", f"task:{task['id']}", "recovered-turn", 0, mode="task", task_id=task["id"])
        receipt = await registry.execute(ctx, call("ccip_delivery_receipt", transfer_id=payment["id"]).calls[0])
        assert receipt["ok"] and receipt["output"]["destination_tx_hash"] == DEST_HASH
        finished = await registry.execute(
            ctx,
            call(
                "finish_task", summary="Finalized destination delivery verified", evidence_ids=[receipt["event_id"]]
            ).calls[0],
        )
        assert finished["ok"]
        saved = recovered.progress("u", task["id"])
        assert saved["task"]["status"] == "completed"
        assert saved["public_state"]["destination_tx"] == DEST_HASH
        assert not any(method.startswith(("eth_send", "wallet_")) for method in calls_after_restart)
        assert sum(method == "eth_sendRawTransaction" for method, _ in chain.calls) == source_sends
        recovered.recover()
        assert recovered.claim() is None
        assert recovered.tasks("u", status="queued", protocol="ccip") == []
        report = {
            "mode": "controlled_rpc",
            "task_id": task["id"],
            "protocol": "ccip",
            "source_transaction": payment["source_tx_hash"],
            "message_id": MESSAGE,
            "destination_transaction": DEST_HASH,
            "recovered_from_empty_database": True,
            "payment_keys_copied": False,
            "payment_journals_copied": False,
            "payment_submissions_before_restart": source_sends,
            "payment_submissions_after_restart": 0,
            "final_status": "completed",
            "verified_at": time.time(),
        }
        with (tmp_path / "clean-ccip-recovery.json").open("w") as report_file:
            json.dump(report, report_file, indent=2)
    finally:
        await observer.close()
        recovered.close()


def test_stale_local_payment_cannot_overwrite_delivered_arkiv_state():
    identity = {"id": "p", "task_id": "t", "user_id": "u", "sender": "s", "intent_id": "i"}
    latest = {**identity, "status": "delivered", "message_id": MESSAGE, "recorded_at_ns": 20}
    stale = {**identity, "status": "awaiting_approval", "recorded_at_ns": 10}
    assert Wallets.resolve_transfer(latest, stale) == latest


async def test_simulation_reuses_arkiv_after_local_result_file_is_lost(config, monkeypatch):
    from tests.test_ccip_simulation import simulation_result

    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    wallets = Wallets(config, store)
    wallets.create("u")
    calls = []

    async def simulate(*args):
        calls.append(1)
        return simulation_result(10**15)

    monkeypatch.setattr(ccip_simulation, "execute", simulate)
    try:
        first = await ccip_simulation.simulate(wallets, "u", RECIPIENT, "0.001")
        ccip_simulation.path(wallets, first["id"]).unlink()
        repeated = await ccip_simulation.simulate(wallets, "u", RECIPIENT, "0.001")
        assert repeated["id"] == first["id"] and repeated["reused_result"]
        assert repeated["storage_status"] == "stored" and calls == [1]
    finally:
        await wallets.close()
        store.close()


@pytest.mark.parametrize(
    "capability,tool", [("file", "write_file"), ("research", "fetch_page"), ("reminder", "wait_until")]
)
def test_capabilities_share_one_task_lifecycle(config, capability, tool):
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    try:
        store.recover()
        task = store.create_task("u", "Work", "Do the work", "Evidence")
        store.event("u", "tool_result", {"tool": tool, "ok": True}, task_id=task["id"])
        store.flush_task(task["id"])
        assert store.progress("u", task["id"])["public_state"]["capability"] == capability
    finally:
        store.close()
