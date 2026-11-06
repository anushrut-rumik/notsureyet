"""Exercise the real wallet signer, Arkiv checkpoints and CCIP state transitions with deterministic RPCs."""

from types import SimpleNamespace

import pytest
from eth_abi import decode, encode
from web3 import Web3

from agent.ccip import (
    DEST_ROUTER,
    DEST_SELECTOR,
    DEST_TOKEN,
    EXEC_TOPIC,
    EXTRA_ARGS,
    SENT_TOPIC,
    SOURCE_ROUTER,
    SOURCE_SELECTOR,
    SOURCE_TOKEN,
    TOKEN_TOPIC,
    calldata,
)
from agent.chat_commands import ChatCommands
from agent.tools import Context, Registry, register_builtins
from agent.wallet import Wallets
from tests.test_agent import call
from tests.test_mobile_wallet import connect
from tests.test_web3 import Chain, approve
from tests.test_web3 import arkiv_store as arkiv_store
from tests.test_web3 import config as config

SENDER = Web3.to_checksum_address("0x" + "45" * 20)
RECIPIENT = "0x" + "12" * 20
OFFRAMP = Web3.to_checksum_address("0x" + "33" * 20)
MESSAGE = "0x" + "ab" * 32
DEST_HASH = "0x" + "de" * 32
BLOCK_HASH = "0x" + "11" * 32


def encoded(types, values):
    return "0x" + encode(types, values).hex()


def topic_address(value):
    return "0x" + int(value, 16).to_bytes(32, "big").hex()


class CCIPChain(Chain):
    def __init__(self):
        super().__init__()
        self.allowance = 0
        self.tokens = 10**21
        self.fee = 10**14
        self.destination_logs = []
        self.destination_receipt = None
        self.finalized = 1000
        self.dest_chain = 84532
        self.wrong_config = False
        self.estimate_failure = False

    async def __call__(self, method, params):
        if method == "eth_getCode":
            return "0x1234"
        if method == "eth_estimateGas" and self.estimate_failure:
            raise ValueError("Cannot estimate yet")
        if method == "eth_getBlockByNumber":
            return {
                "number": hex(self.finalized if params[0] == "finalized" else 100),
                "hash": BLOCK_HASH,
                "transactions": [],
            }
        if method == "eth_call":
            data = params[0]["data"]
            if data.startswith(calldata("router()")):
                return encoded(["address"], [SOURCE_TOKEN if self.wrong_config else SOURCE_ROUTER])
            if data.startswith(calldata("token()")):
                return encoded(["address"], [SOURCE_TOKEN])
            if data.startswith(calldata("destination()")):
                return encoded(["uint64"], [DEST_SELECTOR])
            if data[:10] == calldata("isChainSupported(uint64)")[:10]:
                return encoded(["bool"], [True])
            if data[:10] == calldata("quote(address,uint256,bytes)")[:10]:
                return encoded(["uint256"], [self.fee])
            if data[:10] == calldata("allowance(address,address)")[:10]:
                return encoded(["uint256"], [self.allowance])
            if data[:10] == calldata("balanceOf(address)")[:10]:
                return encoded(["uint256"], [self.tokens])
            raise AssertionError(data)
        return await super().__call__(method, params)

    async def destination(self, method, params):
        if method == "eth_chainId":
            return hex(self.dest_chain)
        if method == "eth_blockNumber":
            return hex(100)
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.finalized), "hash": BLOCK_HASH}
        if method == "eth_call":
            assert params[0]["to"] == DEST_ROUTER
            return encoded(["(uint64,address)[]"], [[(SOURCE_SELECTOR, OFFRAMP)]])
        if method == "eth_getLogs":
            return self.destination_logs
        if method == "eth_getTransactionReceipt":
            return self.destination_receipt
        raise AssertionError(method)

    def confirm(self, payment, status=1):
        super().confirm(payment, status)
        self.transaction["input"] = payment["transaction"]["data"]
        self.nonce = int(payment["transaction"]["nonce"], 16) + 1
        self.receipt["logs"] = []
        if payment["phase"] == "allowance":
            self.allowance = int(payment["amount_units"])
        else:
            self.receipt["logs"] = [
                {
                    "address": SENDER,
                    "topics": [SENT_TOPIC, payment["intent_id"], MESSAGE, topic_address(payment["sender"])],
                    "data": encoded(
                        ["address", "uint256", "uint256"],
                        [payment["recipient"], int(payment["amount_units"]), self.fee],
                    ),
                }
            ]

    def deliver(self, payment, state=2):
        self.finalized = max(200, self.finalized + 1)
        event = {
            "address": OFFRAMP,
            "transactionHash": DEST_HASH,
            "blockNumber": hex(self.finalized),
            "blockHash": BLOCK_HASH,
            "topics": [EXEC_TOPIC, "0x" + SOURCE_SELECTOR.to_bytes(32, "big").hex(), "0x" + "00" * 32, MESSAGE],
            "data": encoded(["uint8", "bytes"], [state, b""]),
        }
        self.destination_logs = [event]
        self.destination_receipt = {
            "transactionHash": DEST_HASH,
            "blockNumber": hex(self.finalized),
            "blockHash": BLOCK_HASH,
            "status": "0x1",
            "logs": [
                event,
                {
                    "address": DEST_TOKEN,
                    "topics": [TOKEN_TOPIC, "0x" + "00" * 32, topic_address(payment["recipient"])],
                    "data": hex(int(payment["amount_units"])),
                },
            ],
        }


@pytest.fixture
async def ccip(config, arkiv_store):
    store, bridge = arkiv_store
    config.env["AGENT_CCIP_SENDER_ADDRESS"] = SENDER
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    chain = CCIPChain()
    wallets = Wallets(config, store, chain)
    wallets.ccip.destination_rpc_override = chain.destination
    wallets.create("u")
    task = store.create_task("u", "Cross-chain test", "Send 1 BnM to Base Sepolia", "Delivered on destination")
    try:
        yield SimpleNamespace(config=config, store=store, bridge=bridge, chain=chain, wallets=wallets, task=task)
    finally:
        await wallets.close()


async def prepare(c):
    return await c.wallets.ccip.prepare("u", c.task["id"], RECIPIENT, "1")


async def submitted_send(c):
    c.chain.allowance = 10**18
    payment = await prepare(c)
    payment = await approve(c.wallets, c.store, payment)
    c.chain.confirm(payment)
    payment = await c.wallets.status("u", payment["id"])
    assert payment["status"] == "in_transit"
    return payment


async def test_allowance_send_delivery_and_duplicate_retry(ccip):
    c = ccip
    p = await prepare(c)
    assert p["phase"] == "allowance" and p["transaction"]["to"] == SOURCE_TOKEN
    assert p["value_wei"] == "0"
    review = ChatCommands.review(p)
    for value in ("1 CCIP-BnM", RECIPIENT, "Base Sepolia", "Maximum CCIP fee", "Maximum gas", "allowance"):
        assert value in review
    assert (await prepare(c))["id"] == p["id"]
    with pytest.raises(ValueError, match="explicit incoming"):
        await c.wallets.approve("u", p["id"])
    approved = await approve(c.wallets, c.store, p)
    c.chain.confirm(approved)
    send = await c.wallets.status("u", p["id"])
    assert send["phase"] == "send" and send["status"] == "awaiting_approval"
    assert send["approval_code"] != p["approval_code"]
    assert send["approval_tx_hash"] == approved["tx_hash"]
    assert send["transaction"]["to"] == SENDER and int(send["value_wei"]) > 0
    old_event = c.store.event(
        "u", "payment_approval", {"intent": c.wallets.fingerprint(p)}, task_id=c.task["id"], source="user"
    )
    with pytest.raises(ValueError, match="exact transfer"):
        await c.wallets.approve("u", p["id"], old_event)
    submitted = await approve(c.wallets, c.store, send)
    c.chain.confirm(submitted)
    pending = await c.wallets.status("u", p["id"])
    assert pending["source_tx_hash"] == submitted["tx_hash"] and pending["message_id"] == MESSAGE
    assert pending["status"] == "in_transit"
    assert (await prepare(c))["id"] == p["id"]
    assert (await approve(c.wallets, c.store, pending))["status"] == "in_transit"
    c.chain.deliver(pending)
    delivered = await c.wallets.status("u", p["id"])
    assert delivered["status"] == "delivered" and delivered["destination_tx_hash"] == DEST_HASH
    await c.wallets.status("u", p["id"])
    assert len([call for call in c.chain.calls if call[0] == "eth_sendRawTransaction"]) == 2
    notices = c.store.events("u", kind="chat_message")
    assert len([e for e in notices if "Delivered 1 CCIP" in e["data"]["content"]]) == 1


async def test_lost_submission_response_recovery_and_sqlite_loss_never_resends(ccip):
    c = ccip
    c.chain.allowance = 10**18
    c.chain.send_failure = True
    p = await approve(c.wallets, c.store, await prepare(c))
    assert p["status"] == "unknown" and p["tx_hash"]
    assert "raw" not in p
    await c.wallets.close()
    c.store.close()
    c.config.db_path.unlink()
    from agent.arkiv import ArkivStore

    c.store = ArkivStore(c.config.db_path, c.config, c.bridge)
    c.store.recover()
    c.wallets = Wallets(c.config, c.store, c.chain)
    c.wallets.ccip.destination_rpc_override = c.chain.destination
    try:
        assert (await prepare(c))["id"] == p["id"]
        await approve(c.wallets, c.store, p)
        c.chain.confirm(p)
        recovered = await c.wallets.status("u", p["id"])
        assert recovered["message_id"] == MESSAGE
        assert len([call for call in c.chain.calls if call[0] == "eth_sendRawTransaction"]) == 1
    finally:
        c.store.close()


async def test_metamask_exact_calldata_once_and_old_step_link_is_disabled(ccip):
    c = ccip
    connect(c.wallets)
    p = await prepare(c)
    reviewed = await approve(c.wallets, c.store, p)
    token = reviewed["confirmation_link"].split("/")[-1]
    assert c.wallets.mobile.info(token)["payment"]["destination"] == "Base Sepolia"
    claim = await c.wallets.mobile.claim(token)
    assert claim["transaction"] == p["transaction"]
    with pytest.raises(ValueError, match="already started"):
        await c.wallets.mobile.claim(token)
    p["tx_hash"] = "0x" + "ce" * 32
    c.chain.confirm(p)
    c.chain.transaction["input"] = "0x"
    with pytest.raises(ValueError, match="does not match"):
        await c.wallets.mobile.attach(token, p["tx_hash"])
    c.chain.confirm(p)
    await c.wallets.mobile.attach(token, p["tx_hash"])
    send = await c.wallets.status("u", p["id"])
    assert send["phase"] == "send"
    with pytest.raises(ValueError, match="earlier payment step"):
        await c.wallets.mobile.attach(token, p["tx_hash"])
    assert not any(method.startswith("eth_send") for method, _ in c.chain.calls)


async def test_failed_destination_execution_remains_pending_and_can_later_deliver(ccip):
    c = ccip
    p = await submitted_send(c)
    c.chain.deliver(p, state=3)
    result = await c.wallets.status("u", p["id"])
    assert result["status"] == "in_transit" and result["last_execution_state"] == 3
    c.chain.deliver(p)
    # A later manual execution gets a new block after the scan cursor.
    c.chain.finalized = 2000
    c.chain.destination_logs[0]["blockNumber"] = hex(1500)
    c.chain.destination_receipt["blockNumber"] = hex(1500)
    assert (await c.wallets.status("u", p["id"]))["status"] == "delivered"


@pytest.mark.parametrize("fault", ["wrong_message", "missing_tokens", "reorg", "wrong_source"])
async def test_unverified_destination_is_never_success(ccip, fault):
    c = ccip
    p = await submitted_send(c)
    c.chain.deliver(p)
    if fault == "wrong_message":
        c.chain.destination_logs[0]["topics"][3] = "0x" + "ff" * 32
    elif fault == "missing_tokens":
        c.chain.destination_receipt["logs"] = []
    elif fault == "reorg":
        c.chain.destination_receipt["blockHash"] = "0x" + "ff" * 32
    else:
        c.chain.destination_logs[0]["topics"][1] = "0x" + "00" * 32
    with pytest.raises(ValueError):
        await c.wallets.status("u", p["id"])
    assert c.wallets.load("u", p["id"])["status"] == "in_transit"


async def test_source_finality_required_even_if_destination_event_exists(ccip):
    c = ccip
    p = await submitted_send(c)
    c.chain.deliver(p)
    c.chain.finalized = 99
    assert (await c.wallets.status("u", p["id"]))["status"] == "in_transit"


async def test_wrong_config_fee_cap_insufficient_tokens_and_task_collision(ccip):
    c = ccip
    c.chain.wrong_config = True
    with pytest.raises(ValueError, match="verified"):
        await prepare(c)
    c.chain.wrong_config = False
    c.chain.dest_chain = 1
    with pytest.raises(ValueError, match="Base Sepolia"):
        await prepare(c)
    c.chain.dest_chain = 84532
    c.chain.fee = 10**18
    with pytest.raises(ValueError, match="fee exceeds"):
        await prepare(c)
    c.chain.fee = 10**14
    c.chain.tokens = 0
    with pytest.raises(ValueError, match="Insufficient CCIP-BnM"):
        await prepare(c)
    c.chain.tokens = 10**21
    await prepare(c)
    with pytest.raises(ValueError, match="different payment intent"):
        await c.wallets.ccip.prepare("u", c.task["id"], RECIPIENT, "2")
    with pytest.raises(ValueError, match="different payment intent"):
        await c.wallets.prepare("u", c.task["id"], RECIPIENT, "0.001")


async def test_checkpoint_failure_between_allowance_and_send_is_recoverable(ccip):
    c = ccip
    p = await approve(c.wallets, c.store, await prepare(c))
    c.chain.confirm(p)
    c.chain.estimate_failure = True
    with pytest.raises(ValueError, match="estimate"):
        await c.wallets.status("u", p["id"])
    assert c.wallets.load("u", p["id"])["status"] == "allowance_confirmed"
    c.chain.estimate_failure = False
    recovered = await c.wallets.status("u", p["id"])
    assert recovered["phase"] == "send" and recovered["status"] == "awaiting_approval"
    assert recovered["approval_tx_hash"] == p["tx_hash"]
    assert len([call for call in c.chain.calls if call[0] == "eth_sendRawTransaction"]) == 1


async def test_completion_requires_delivery_evidence(ccip):
    from agent.ccip import register_ccip_tools

    c = ccip
    p = await submitted_send(c)
    registry = Registry()
    register_builtins(registry)
    register_ccip_tools(registry)
    ctx = Context(SimpleNamespace(store=c.store, wallets=c.wallets), "u", "task", c.task["id"], 1, "task", c.task["id"])
    event = c.store.event("u", "tool_result", {"ok": True, "evidence": True}, task_id=c.task["id"])
    result = await registry.execute(ctx, call("finish_task", summary="Done", evidence_ids=[event]).calls[0])
    assert not result["ok"]
    result = await registry.execute(ctx, call("ccip_delivery_receipt", transfer_id=p["id"]).calls[0])
    assert not result["ok"]
    c.chain.deliver(p)
    result = await registry.execute(ctx, call("ccip_delivery_receipt", transfer_id=p["id"]).calls[0])
    assert result["ok"]
    result = await registry.execute(
        ctx, call("finish_task", summary="Delivered", evidence_ids=[result["event_id"]]).calls[0]
    )
    assert result["ok"] and c.store.task("u", c.task["id"])["status"] == "completed"


def test_default_extra_args_is_v3_finalized():
    assert EXTRA_ARGS == "0xa69dd4aa" + "00" * 15
    assert decode(
        ["address", "uint256"],
        bytes.fromhex(calldata("approve(address,uint256)", ["address", "uint256"], [SENDER, 10**18])[10:]),
    ) == (SENDER.lower(), 10**18)


async def test_refresh_keeps_intent_and_rotates_approval_but_refuses_after_signing(ccip):
    c = ccip
    connect(c.wallets)
    original = await prepare(c)
    reviewed = await approve(c.wallets, c.store, original)
    old_token = reviewed["confirmation_link"].split("/")[-1]
    refreshed = await c.wallets.ccip.refresh("u", original["id"])
    assert refreshed["intent_id"] == original["intent_id"]
    assert refreshed["id"] == original["id"] and refreshed["approval_code"] != original["approval_code"]
    # Identical calldata can still have a new review: old link may not claim the fresh authorization.
    reviewed = await approve(c.wallets, c.store, refreshed)
    new_token = reviewed["confirmation_link"].split("/")[-1]
    with pytest.raises(ValueError, match="earlier payment step"):
        await c.wallets.mobile.claim(old_token)
    await c.wallets.mobile.claim(new_token)
    with pytest.raises(ValueError, match="unsigned"):
        await c.wallets.ccip.refresh("u", original["id"])


async def test_normal_tracking_does_not_write_checkpoint_for_each_poll(ccip):
    c = ccip
    payment = await submitted_send(c)
    writes = len(c.bridge.rows)
    for _ in range(3):
        assert (await c.wallets.status("u", payment["id"]))["status"] == "in_transit"
    assert len(c.bridge.rows) == writes


async def test_natural_language_task_uses_existing_raw_chat_approval(config):
    from agent.arkiv import ArkivStore
    from agent.providers import Answer
    from agent.runtime import Runtime
    from tests.test_agent import Script, last_result
    from tests.test_web3 import MemoryArkiv

    config.env["AGENT_CCIP_SENDER_ADDRESS"] = SENDER
    models = Script(
        call(
            "create_task",
            title="CCIP transfer",
            instruction="Send 1 BnM to Base Sepolia",
            done_when="Destination delivered",
        ),
        Answer("I will prepare the transfer for your review."),
        call("prepare_ccip_transfer", recipient=RECIPIENT, amount="1"),
        lambda _role, messages, _tools: call("ask_user", question=last_result(messages)["output"]["review_message"]),
    )
    runtime = Runtime(config, models)
    runtime.store.close()
    runtime.store = ArkivStore(config.db_path, config, MemoryArkiv())
    runtime.engine.store = runtime.wallets.store = runtime.wallets.ccip.store = runtime.store
    chain = CCIPChain()
    chain.allowance = 10**18
    runtime.wallets.rpc_override = chain
    runtime.wallets.ccip.destination_rpc_override = chain.destination
    try:
        await runtime.chat("u", "/wallet create")
        await runtime.chat("u", f"Send 1 CCIP-BnM from Ethereum Sepolia to {RECIPIENT} on Base Sepolia")
        await runtime.run_once()
        [payment] = runtime.wallets.list("u")
        assert runtime.store.task("u", payment["task_id"])["status"] == "needs_input"
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
        response = await runtime.chat("u", "approve " + payment["approval_code"])
        assert "submitted" in response
        payment = runtime.wallets.load("u", payment["id"])
        chain.confirm(payment)
        pending = await runtime.wallets.status("u", payment["id"])
        assert pending["status"] == "in_transit"
        assert runtime.store.task("u", payment["task_id"])["status"] != "completed"
        chain.deliver(pending)
        await runtime.wallets.status("u", payment["id"])
        models.steps.extend(
            [
                call("ccip_delivery_receipt", transfer_id=payment["id"]),
                lambda _r, messages, _t: call(
                    "finish_task", summary="Delivered on Base Sepolia", evidence_ids=[last_result(messages)["event_id"]]
                ),
            ]
        )
        await runtime.run_once()
        assert runtime.store.task("u", payment["task_id"])["status"] == "completed"
        assert len([c for c in chain.calls if c[0] == "eth_sendRawTransaction"]) == 1
    finally:
        await runtime.close()


def test_token_amounts_do_not_round_in_review_or_calldata():
    from agent.ccip import decimal, token_units

    amount = "12345678901234567890.123456789012345678"
    units = token_units(amount)
    assert units == 12345678901234567890123456789012345678
    assert decimal(units) == amount


async def test_arkiv_signatures_can_release_a_payment_checkpoint_barrier(ccip, monkeypatch):
    """Storage signing must remain available while payment authorization holds its wallet lock."""
    import asyncio
    import hashlib
    import time

    c = ccip
    token = "pending-storage-signature"
    mobile = c.wallets.mobile
    mobile._save(
        hashlib.sha256(token.encode()).hexdigest(),
        {
            "kind": "arkiv",
            "user_id": "u",
            "created_at": time.time(),
            "expires_at": time.time() + 300,
            "claimed": False,
            "operation": "op",
            "instance": "scope",
        },
    )
    tx = {
        "from": c.wallets.get("u")["address"],
        "to": SENDER,
        "value": "0x0",
        "data": "0x1234",
        "gas": "0x5208",
        "gasPrice": "0x1",
    }
    journal = {
        "state": "external_prepared",
        "instance": "scope",
        "operation": "op",
        "transaction": tx,
        "task": c.task["id"],
    }
    bridge = SimpleNamespace(
        scope=lambda user: {"writer": tx["from"]},
        journal=lambda scope: journal,
        claim_external=lambda user: {"transaction": tx},
        attach_external=lambda user, tx_hash: None,
    )
    monkeypatch.setattr(c.store, "bridge", bridge)
    monkeypatch.setattr(c.store, "accept_checkpoint", lambda user: None)
    async with c.wallets.lock:
        claimed = await asyncio.wait_for(mobile.claim(token), timeout=1)
        assert claimed["transaction"] == tx
        result = await asyncio.wait_for(mobile.attach(token, DEST_HASH), timeout=1)
        assert result["saved"]
