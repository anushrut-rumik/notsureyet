import json
from uuid import uuid4

import httpx
import pytest

from agent.arkiv import ArkivError, ArkivStore
from agent.config import Config
from agent.providers import Answer
from agent.runtime import Runtime
from agent.server import create_app
from agent.store import Store
from agent.vault import Vault
from agent.wallet import Wallets, amount_wei
from tests.test_agent import Script, call


class MemoryArkiv:
    """Test substitute for chain storage, shared between fresh SQLite instances."""

    instance = "test-instance"

    def __init__(self):
        self.rows = []
        self.reads = 0
        self.fail = False

    def call(self, command, **args):
        if self.fail:
            raise ArkivError("Simulated unavailable Arkiv")
        if command == "read":
            self.reads += 1
            return {"rows": [row for row in self.rows if not args.get("task") or row["task"] == args["task"]]}
        if command == "write":
            row = {**args, "entity_key": "0x" + uuid4().hex * 2, "expires_at_block": "123456"}
            self.rows.append(row)
            return row
        return {"reconciled": True}

    def close(self):
        pass


@pytest.fixture
def config(tmp_path):
    (tmp_path / "inference.toml").write_text(
        '[routes]\ndefault="test"\n[endpoints.test]\nbase_url="http://unused/v1"\nmodel="test"\n'
    )
    return Config(tmp_path, environ={"AGENT_TASK_BACKEND": "sqlite"})


class Chain:
    def __init__(self):
        self.calls = []
        self.hash = None
        self.transaction = None
        self.receipt = None
        self.send_failure = False
        self.chain = 11155111
        self.balance = 10**18
        self.nonce = 0

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_getCode":
            return "0x"
        if method == "eth_blockNumber":
            return "0x65"
        if method == "eth_getTransactionCount":
            return hex(self.nonce)
        if method == "eth_gasPrice":
            return hex(10**9)
        if method == "eth_estimateGas":
            return hex(21000)
        if method == "eth_sendRawTransaction":
            from web3 import Web3

            self.hash = Web3.keccak(hexstr=params[0]).to_0x_hex()
            if self.send_failure:
                raise ValueError("Lost response after provider accepted the transaction")
            return self.hash
        if method == "eth_getTransactionByHash":
            return self.transaction
        if method == "eth_getTransactionReceipt":
            return self.receipt
        if method == "eth_getBlockByNumber":
            return {"hash": "0x" + "11" * 32, "transactions": []}
        raise AssertionError(method)

    def confirm(self, payment, status=1):
        tx = payment["transaction"]
        self.transaction = {**tx, "input": "0x", "hash": payment["tx_hash"]}
        self.receipt = {
            "transactionHash": payment["tx_hash"],
            "blockNumber": "0x64",
            "blockHash": "0x" + "11" * 32,
            "status": hex(status),
        }


async def approve(wallets, store, payment):
    event_id = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=payment["task_id"], source="user"
    )
    return await wallets.approve("u", payment["id"], event_id)


@pytest.fixture
def arkiv_store(config):
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    yield store, bridge
    store.close()


def test_arkiv_list_queries_remote_and_fails_closed(arkiv_store):
    store, bridge = arkiv_store
    task = store.create_task("u", "Payment", "Pay", "Receipt confirmed")
    assert "Payment" not in json.dumps(bridge.rows)
    before = bridge.reads
    assert store.tasks("u")[0]["id"] == task["id"]
    assert bridge.reads == before + 1
    assert store.tasks("other") == []
    bridge.fail = True
    with pytest.raises(ArkivError):
        store.tasks("u")
    with pytest.raises(ArkivError):
        store.update_task("u", task["id"], notes="Not durable")
    assert store.claim() is None


async def test_sqlite_loss_restores_task_messages_effects_and_payment_without_resend(config):
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    chain = Chain()
    wallets = Wallets(config, store, chain)
    wallets.create("u")
    task = store.create_task("u", "Pay", "Pay once", "A confirmed receipt")
    store.message(f"task:{task['id']}", {"role": "user", "content": "Pay once"})
    store.effect_start("effect-1", "u", task["id"], "action")
    payment = await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
    chain.send_failure = True
    payment = await approve(wallets, store, payment)
    assert payment["status"] == "unknown"
    assert payment["tx_hash"] == chain.hash
    assert "raw" not in payment
    await wallets.close()
    store.close()
    # Keep vault.key, discard the complete task/execution database.
    config.db_path.unlink()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    wallets = Wallets(config, store, chain)
    try:
        assert store.task("u", task["id"])["title"] == "Pay"
        assert store.messages(f"task:{task['id']}")[0]["content"] == "Pay once"
        assert store.effect("effect-1")["status"] == "unknown"
        assert wallets.load("u", payment["id"])["tx_hash"] == chain.hash
        chain.confirm(payment)
        assert (await wallets.status("u", payment["id"]))["status"] == "confirmed"
        assert (await approve(wallets, store, payment))["status"] == "confirmed"
        assert len([c for c in chain.calls if c[0] == "eth_sendRawTransaction"]) == 1
    finally:
        await wallets.close()
        store.close()


async def test_wallet_custody_and_watch_only_connection(config):
    store = Store(config.db_path)
    wallets = Wallets(config, store, Chain())
    try:
        watched = wallets.watch("u", "0x" + "12" * 20)
        assert watched["kind"] == "watch" and not watched["can_sign"]
        local = wallets.create("u")
        assert local["kind"] == "local" and "secret" not in local
        with pytest.raises(ValueError, match="already"):
            wallets.create("u")
        with pytest.raises(ValueError, match="configured"):
            wallets.watch("u", "0x" + "34" * 20)
        assert (await wallets.balance("u"))["balance"] == "1"
    finally:
        await wallets.close()
        store.close()


async def test_approval_and_broadcast_are_blocked_if_checkpoint_fails(config, arkiv_store):
    store, bridge = arkiv_store
    chain = Chain()
    wallets = Wallets(config, store, chain)
    try:
        wallets.create("u")
        task = store.create_task("u", "Pay", "Pay", "Receipt")
        payment = await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        bridge.fail = True
        with pytest.raises(ArkivError):
            await approve(wallets, store, payment)
        assert not any(method == "eth_sendRawTransaction" for method, _ in chain.calls)
    finally:
        await wallets.close()


async def test_receipt_must_match_and_revert_is_never_success(config, arkiv_store):
    store, _bridge = arkiv_store
    chain = Chain()
    wallets = Wallets(config, store, chain)
    try:
        wallets.create("u")
        task = store.create_task("u", "Pay", "Pay", "Receipt")
        payment = await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        assert not any(method == "eth_sendRawTransaction" for method, _ in chain.calls)
        payment = await approve(wallets, store, payment)
        chain.confirm(payment)
        chain.transaction["value"] = "0x1"
        with pytest.raises(ValueError, match="does not match"):
            await wallets.status("u", payment["id"])
        chain.confirm(payment, status=0)
        assert (await wallets.status("u", payment["id"]))["status"] == "reverted"
    finally:
        await wallets.close()


async def test_payment_intent_is_unique_and_signing_requires_real_approval(config, arkiv_store):
    store, bridge = arkiv_store
    chain = Chain()
    wallets = Wallets(config, store, chain)
    try:
        wallets.create("u")
        task = store.create_task("u", "Pay", "Pay", "Receipt")
        payment = await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        assert (await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001"))["id"] == payment["id"]
        with pytest.raises(ValueError, match="incoming approval"):
            await wallets.approve("u", payment["id"])
        with pytest.raises(ValueError, match="different payment"):
            await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.002")
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
    finally:
        await wallets.close()


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1.0000000000000000001"])
def test_invalid_amounts(value):
    with pytest.raises(ValueError):
        amount_wei(value)


async def test_local_mode_cannot_send_or_expose_approval_as_a_model_tool(config):
    runtime = Runtime(config, Script())
    try:
        tools = {tool["function"]["name"] for tool in runtime.registry.schemas("task")}
        assert "prepare_transfer" in tools and "confirmed_transfer_receipt" in tools
        assert not {"approve", "sign", "submit", "connect_wallet"}.intersection(tools)
        task = runtime.store.create_task("u", "Pay", "Pay", "Receipt")
        with pytest.raises(ValueError, match="require"):
            await runtime.wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
    finally:
        await runtime.close()


def test_vault_rejects_tamper_wrong_context_and_lost_key(tmp_path):
    vault = Vault(tmp_path)
    encrypted = vault.seal({"secret": "private"}, "task:one")
    assert vault.open(encrypted, "task:one") == {"secret": "private"}
    with pytest.raises(ValueError):
        vault.open(encrypted, "task:two")
    (tmp_path / "arkiv-writer.json").write_text("{}")
    (tmp_path / "vault.key").unlink()
    with pytest.raises(ValueError, match="missing"):
        Vault(tmp_path)


async def test_chat_commands_approve_only_matching_chat_and_never_from_model(config):
    runtime = Runtime(config, Script())
    runtime.store.close()
    bridge = MemoryArkiv()
    runtime.store = ArkivStore(config.db_path, config, bridge)
    runtime.engine.store = runtime.store
    runtime.wallets.store = runtime.store
    runtime.recovered = False
    chain = Chain()
    runtime.wallets.rpc_override = chain
    try:
        reply = await runtime.chat("u", "/wallet create")
        assert "Your agent wallet" in reply and "tGLM" in reply
        reply = await runtime.chat("u", "/transfer 0.001 0x" + "12" * 20)
        assert "Reply exactly: approve" in reply
        payment = runtime.wallets.list("u")[0]
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
        assert "No payment" in await runtime.chat("other", "approve " + payment["approval_code"])
        assert "unknown" not in await runtime.chat("u", "approve " + payment["approval_code"])
        await runtime.chat("u", "approve " + payment["approval_code"])
        assert len([c for c in chain.calls if c[0] == "eth_sendRawTransaction"]) == 1
        assert not runtime.models.calls  # approvals never enter model inference
        wallet = runtime.wallets.get("u")
        assert wallet["arkiv"]["address"] == wallet["address"]
    finally:
        await runtime.close()


async def test_chat_api_has_no_wallet_ui_or_signing_endpoint(config):
    app = create_app(config, models=Script())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            reply = await client.post("/chat", json={"message": "/wallet create"})
            assert reply.status_code == 200
            assert "Your agent wallet" in reply.json()["reply"]
            assert (await client.get("/")).status_code == 404
            assert (await client.post("/wallet/approve")).status_code == 404


async def test_model_prepares_human_approves_and_model_finishes_only_after_receipt(config):
    from agent.tools import Context
    from tests.test_agent import last_result

    models = Script(
        call("create_task", title="Pay once", instruction="Pay the requested recipient", done_when="Confirmed payment"),
        Answer("I'll prepare the payment for your review."),
        call("prepare_transfer", recipient="0x" + "12" * 20, amount_eth="0.001"),
        lambda _role, messages, _tools: call("ask_user", question=last_result(messages)["output"]["review_message"]),
    )
    runtime = Runtime(config, models)
    runtime.store.close()
    runtime.store = ArkivStore(config.db_path, config, MemoryArkiv())
    runtime.engine.store = runtime.store
    runtime.wallets.store = runtime.store
    chain = Chain()
    runtime.wallets.rpc_override = chain
    try:
        await runtime.chat("u", "/wallet create")
        await runtime.chat("u", "Pay 0.001 ETH to the recipient")
        await runtime.run_once()
        [payment] = runtime.wallets.list("u")
        assert runtime.store.task("u", payment["task_id"])["status"] == "needs_input"
        # A plausible successful balance observation cannot complete a payment.
        event_id = runtime.store.event("u", "tool_result", {"ok": True, "evidence": True}, task_id=payment["task_id"])
        ctx = Context(runtime.engine, "u", "task", payment["task_id"], 1, "task", payment["task_id"])
        result = await runtime.registry.execute(
            ctx, call("finish_task", summary="Paid", evidence_ids=[event_id]).calls[0]
        )
        assert not result["ok"]
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
        await runtime.chat("u", "approve " + payment["approval_code"])
        payment = runtime.wallets.load("u", payment["id"])
        chain.confirm(payment)
        await runtime.wallets.status("u", payment["id"])
        models.steps.extend(
            [
                call("confirmed_transfer_receipt", transfer_id=payment["id"]),
                lambda _r, messages, _t: call(
                    "finish_task",
                    summary="Payment confirmed on Sepolia",
                    evidence_ids=[last_result(messages)["event_id"]],
                ),
            ]
        )
        await runtime.run_once()
        assert runtime.store.tasks("u")[0]["status"] == "completed"
        assert len([c for c in chain.calls if c[0] == "eth_sendRawTransaction"]) == 1
    finally:
        await runtime.close()


async def test_ira_whatsapp_contract_identity_dedup_and_background_notices(config):
    from agent.whatsapp import IraWhatsApp

    config.env.update(
        {
            "AGENT_WHATSAPP_LINE": "line-hackathon",
            "AGENT_WHATSAPP_ALLOWED_CHATS": "123@s.whatsapp.net",
            "AGENT_WHATSAPP_SIDECAR_URL": "http://sidecar",
            "AGENT_WHATSAPP_SECRET": "test-secret",
            "AGENT_WHATSAPP_REDIS_URL": "redis://unused",
        }
    )
    runtime = Runtime(config, Script())
    sent = []

    def respond(request):
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path == "/send/line-hackathon":
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "wa-reply-1", "chat": "123@s.whatsapp.net"})

    class Redis:
        async def aclose(self):
            pass

    adapter = IraWhatsApp(
        runtime,
        redis=Redis(),
        http=httpx.AsyncClient(
            base_url="http://sidecar",
            headers={"Authorization": "Bearer test-secret"},
            transport=httpx.MockTransport(respond),
        ),
    )
    event = {
        "type": "message",
        "user": "line-hackathon",
        "line": True,
        "id": "wa-in-1",
        "chat": "123@s.whatsapp.net",
        "sender_lid": "456@lid",
        "text": "/wallet create",
    }
    try:
        assert not await adapter.receive({**event, "is_group": True})
        assert not await adapter.receive({**event, "history": True})
        assert not await adapter.receive({**event, "forwarded": True})
        assert not await adapter.receive({**event, "sender_lid": ""})
        assert await adapter.receive(event)
        assert not await adapter.receive(event)
        await adapter.deliver_once()
        await adapter.deliver_once()
        assert len(sent) == 1 and "Your agent wallet" in sent[0]["text"]
        user = adapter.db.execute("SELECT user_id FROM routes").fetchone()[0]
        runtime.store.notice(user, "Payment confirmed on Sepolia")
        await adapter.deliver_once()
        assert len(sent) == 2 and sent[1]["text"] == "Payment confirmed on Sepolia"
    finally:
        await adapter.close()
        await runtime.close()


async def test_linking_uses_sidecar_and_never_replaces_an_existing_number(config):
    from agent.whatsapp_setup import setup

    config.env.update(
        {
            "AGENT_WHATSAPP_LINE": "line-test",
            "AGENT_WHATSAPP_SIDECAR_URL": "http://sidecar",
            "AGENT_WHATSAPP_SECRET": "private-test-secret",
            "AGENT_WHATSAPP_PHONE": "+917710001525",
        }
    )
    calls = []

    def response(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer private-test-secret"
        if request.method == "GET":
            return httpx.Response(200, json={"linked": False, "state": "unlinked"})
        assert json.loads(request.content) == {"phone": "+917710001525"}
        return httpx.Response(200, json={"code": "ABCD-EFGH", "qr": "never-print-this"})

    result = await setup(config, transport=httpx.MockTransport(response))
    assert result["code"] == "ABCD-EFGH" and "qr" not in result
    assert [r.url.path for r in calls] == ["/status/line-test", "/link/line-test"]

    def already_linked(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"linked": True, "phone": "+919999999999", "state": "connected"})

    with pytest.raises(ValueError, match="different number"):
        await setup(config, transport=httpx.MockTransport(already_linked))


async def test_whatsapp_leaves_empty_pending_page_and_keeps_event_ids_after_cache_loss(config):
    import asyncio

    from agent.whatsapp import IraWhatsApp

    config.env.update(
        {
            "AGENT_WHATSAPP_LINE": "line-test",
            "AGENT_WHATSAPP_ALLOWED_CHATS": "123@s.whatsapp.net",
            "AGENT_WHATSAPP_SIDECAR_URL": "http://sidecar",
            "AGENT_WHATSAPP_SECRET": "test-secret",
            "AGENT_WHATSAPP_REDIS_URL": "redis://unused",
        }
    )

    class Redis:
        def __init__(self):
            self.positions = []

        async def xgroup_create(self, *args, **kwargs):
            pass

        async def xreadgroup(self, group, consumer, streams, **kwargs):
            self.positions.append(list(streams.values())[0])
            if len(self.positions) == 1:
                return [("stream", [])]
            raise asyncio.CancelledError

        async def aclose(self):
            pass

    runtime = Runtime(config, Script())
    redis = Redis()
    adapter = IraWhatsApp(runtime, redis=redis, http=httpx.AsyncClient())
    try:
        with pytest.raises(asyncio.CancelledError):
            await adapter.run()
        assert redis.positions == ["0", ">"]
        with adapter.db:
            adapter.db.execute("INSERT INTO deliveries VALUES(900,'accepted','old-reply')")
    finally:
        await adapter.close()
        await runtime.close()
    config.db_path.unlink()
    runtime = Runtime(config, Script())
    adapter = IraWhatsApp(runtime, redis=Redis(), http=httpx.AsyncClient())
    try:
        assert runtime.store.notice("u", "New reply after recovery") > 900
    finally:
        await adapter.close()
        await runtime.close()
