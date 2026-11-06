"""Wallet ownership, user isolation and approval barriers using simulated network storage."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from eth_account import Account

from agent.arkiv import ArkivError, ArkivStore, Bridge, initialize
from agent.vault import Vault
from agent.wallet import Wallets
from tests.test_web3 import config as config


class WalletNetwork(Bridge):
    def __init__(self, config, rows):
        super().__init__(config, Vault(config.data_dir))
        self.rows = rows

    def _invoke(self, scope, command, **args):
        if command == "read":
            return {"rows": [r for r in self.rows if r["instance"] == scope["instance"]]}
        if command == "reconcile":
            return {"reconciled": True}
        if command == "funding":
            return {"balance_wei": "10000000000000000"}
        if command == "prepare_external":
            journal = {
                **args,
                **scope,
                "state": "external_prepared",
                "operation": uuid4().hex,
                "transaction": {"to": "0x" + "44" * 20, "gas": "0xf4240", "gasPrice": "0x1"},
            }
            self.save_json(self.journal_path(scope), journal)
            return {"pending": True}
        if command == "write":
            row = {**args, **scope, "entity_key": "0x" + uuid4().hex * 2, "expires_at_block": "12345"}
            self.rows.append(row)
            return row
        if command == "claim_external":
            journal = self.journal(scope)
            if journal["state"] != "external_prepared":
                raise ArkivError("Request already started")
            self.save_json(self.journal_path(scope), {**journal, "state": "external_claimed"})
            return {"transaction": journal["transaction"]}
        if command == "attach_external":
            journal = self.journal(scope)
            row = {**journal, "entity_key": "0x" + uuid4().hex * 2, "expires_at_block": "12345"}
            self.rows.append(row)
            self.save_json(
                self.journal_path(scope), {**journal, "state": "verified", "result": row, "hash": args["tx_hash"]}
            )
            return row
        raise AssertionError(command)


async def test_two_created_wallets_own_separate_tasks_and_recover_after_cache_loss(config):
    rows = []
    bridge = WalletNetwork(config, rows)
    store = ArkivStore(config.db_path, config, bridge)
    wallets = Wallets(config, store)
    try:
        store.recover()
        alice, bob = wallets.create("alice"), wallets.create("bob")
        a = store.create_task("alice", "Alice private", "Write A", "Done")
        b = store.create_task("bob", "Bob private", "Write B", "Done")
        assert rows[0]["writer"] == alice["address"]
        assert rows[1]["writer"] == bob["address"]
        assert rows[0]["instance"] != rows[1]["instance"]
        assert [t["id"] for t in store.tasks("alice")] == [a["id"]]
        assert [t["id"] for t in store.tasks("bob")] == [b["id"]]
        assert not (config.data_dir / "arkiv-writer.json").exists()
    finally:
        await wallets.close()
        store.close()
    config.db_path.unlink()
    bridge = WalletNetwork(config, rows)
    store = ArkivStore(config.db_path, config, bridge)
    try:
        store.recover()
        assert store.tasks("alice")[0]["title"] == "Alice private"
        assert store.tasks("bob")[0]["title"] == "Bob private"
    finally:
        store.close()


async def test_new_wallet_selection_applies_to_new_tasks_without_reassigning_old_ones(config):
    rows = []
    bridge = WalletNetwork(config, rows)
    store = ArkivStore(config.db_path, config, bridge)
    wallets = Wallets(config, store)
    try:
        store.recover()
        original = wallets.create("u")
        old = store.create_task("u", "Original", "Work", "Done")
        connected = Account.create()
        path = config.data_dir / ("metamask-" + hashlib.sha256(b"u").hexdigest() + ".json")
        path.write_text(json.dumps({"user_id": "u", "address": connected.address, "kind": "metamask"}))
        new = store.create_task("u", "Connected", "Work", "Done")
        assert bridge.pending("u")["writer"] == connected.address
        assert bridge.pending("u")["task"] == new["id"]
        assert rows[0]["writer"] == original["address"]
        assert store.tasks("u")[0]["id"] == old["id"]  # Pending local records are never presented as stored.
        with pytest.raises(ArkivError, match="previously selected"):
            bridge.scope_for_task("u", old["id"])
        with pytest.raises(ValueError, match="already connected"):
            wallets.create("u")
    finally:
        await wallets.close()
        store.close()


async def test_metamask_checkpoint_waits_without_blocking_signing_http_and_binds_links(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    rows = []
    bridge = WalletNetwork(config, rows)
    store = ArkivStore(config.db_path, config, bridge)
    wallets = Wallets(config, store)
    from tests.test_mobile_wallet import connect

    try:
        store.recover()
        connect(wallets)
        task = store.create_task("u", "Save me", "Work", "Done")
        assert store.claim() is None
        assert not store.tasks("u")
        token = wallets.mobile.issue("u", "arkiv")["url"].split("/")[-1]
        blocked = asyncio.create_task(store.checkpoint_barrier(task["id"]))
        await asyncio.sleep(0)
        assert not blocked.done()
        assert wallets.mobile.info(token)["payment"]["sender"] == wallets.get("u")["address"]
        await wallets.mobile.claim(token)
        with pytest.raises(ValueError, match="already started"):
            await wallets.mobile.claim(token)
        await wallets.mobile.attach(token, "0x" + "12" * 32)
        await asyncio.wait_for(blocked, 2)
        assert store.tasks("u")[0]["id"] == task["id"]
        store.update_task("u", task["id"], notes="New revision")
        with pytest.raises(ValueError, match="No Arkiv checkpoint"):
            wallets.mobile.info(token)
    finally:
        await wallets.close()
        store.close()


async def test_pending_unsigned_checkpoint_survives_restart_and_never_runs_early(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    rows = []
    bridge = WalletNetwork(config, rows)
    store = ArkivStore(config.db_path, config, bridge)
    wallets = Wallets(config, store)
    from tests.test_mobile_wallet import connect

    try:
        store.recover()
        connect(wallets)
        task = store.create_task("u", "Pending", "Still waiting", "Done")
    finally:
        await wallets.close()
        store.close()
    config.db_path.unlink()
    bridge = WalletNetwork(config, rows)
    store = ArkivStore(config.db_path, config, bridge)
    try:
        store.recover()
        assert store.task("u", task["id"])["title"] == "Pending"
        assert not store.tasks("u")
        assert store.claim() is None
    finally:
        store.close()


async def test_actual_bridge_passes_selected_wallet_key_to_sdk_not_a_global_writer(config, monkeypatch):
    vault = Vault(config.data_dir)
    bridge = Bridge(config, vault)
    store = ArkivStore(config.db_path, config, bridge)
    wallets = Wallets(config, store)
    requests = []

    def run(_command, **options):
        request = json.loads(options["input"])
        requests.append(request)
        return SimpleNamespace(stdout=json.dumps({"ok": True, "result": {"entity_key": "example"}}))

    monkeypatch.setattr("agent.arkiv.subprocess.run", run)
    try:
        for user in ("alice", "bob"):
            wallet = wallets.create(user)
            bridge.call("write", user_id=user, task=user + "-task", revision="1", encrypted="test")
            assert Account.from_key(requests[-1]["private_key"]).address == wallet["address"]
        assert requests[0]["writer"] != requests[1]["writer"]
        assert requests[0]["journal"] != requests[1]["journal"]
        assert initialize(config)["network"] == "Rabdi"
        assert not (config.data_dir / "arkiv-writer.json").exists()
    finally:
        await wallets.close()
        store.close()


async def test_external_effect_waits_for_checkpoint_approval_before_touching_a_file(config):
    from agent.runtime import Runtime
    from agent.tools import Context
    from tests.test_agent import Script, call
    from tests.test_mobile_wallet import connect

    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    runtime = Runtime(config, Script())
    runtime.store.close()
    bridge = WalletNetwork(config, [])
    runtime.store = ArkivStore(config.db_path, config, bridge)
    runtime.wallets.store = runtime.store
    runtime.engine.store = runtime.store
    operation = None
    try:
        runtime.store.recover()
        connect(runtime.wallets)
        task = runtime.store.create_task("u", "Write once", "Write proof.txt", "Written")
        bridge.attach_external("u", "0x" + "11" * 32)
        runtime.store.accept_checkpoint("u")
        event = runtime.store.event("u", "chat_message", {"content": "Write proof.txt"}, source="user")
        ctx = Context(runtime.engine, "u", f"task:{task['id']}", "turn", event, "task", task["id"])
        operation = asyncio.create_task(
            runtime.registry.execute(
                ctx, call("write_file", filename="proof.txt", content="Only after approval").calls[0]
            )
        )
        await asyncio.sleep(0.05)
        assert not operation.done()
        assert not (config.workspace / "proof.txt").exists()
        for i in range(8):
            if operation.done():
                break
            if bridge.pending("u"):
                bridge.attach_external("u", "0x" + f"{i + 2:02x}" * 32)
                runtime.store.accept_checkpoint("u")
            await asyncio.sleep(0.55)
        result = await asyncio.wait_for(operation, 1)
        assert result["ok"]
        assert (config.workspace / "proof.txt").read_text() == "Only after approval"
        effects = runtime.store._snapshot(task["id"])["effects"]
        assert len(effects) == 1 and effects[0]["status"] == "succeeded"
    finally:
        if operation and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


async def test_storage_outage_keeps_wallet_setup_available_and_defers_recovery(config):
    from agent.runtime import Runtime
    from tests.test_agent import Script

    runtime = Runtime(config, Script())
    attempts = []

    def unavailable():
        attempts.append(1)
        raise ArkivError("Public RPC is rate limited")

    runtime.store.recover = unavailable
    try:
        reply = await runtime.chat("u", "/wallet create")
        assert "Your agent wallet" in reply
        assert not runtime.recovered
        assert runtime.recovery_error == "ArkivError"
        assert runtime.recover(strict=False) is False
        assert len(attempts) == 1
    finally:
        await runtime.close()
