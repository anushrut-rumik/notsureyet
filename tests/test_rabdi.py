"""Native Rabdi payments, nonce sharing with storage, and recovery without another send."""

import asyncio

import pytest
from eth_account import Account

from agent.arkiv import ArkivStore
from agent.chat_commands import ChatCommands
from agent.rabdi import RABDI, payment_reserved
from agent.wallet import Wallets
from tests.test_arkiv_wallets import WalletNetwork
from tests.test_mobile_wallet import connect
from tests.test_web3 import Chain, approve
from tests.test_web3 import config as config


class RabdiChain(Chain):
    def __init__(self):
        super().__init__()
        self.chain = RABDI
        self.head = 101
        self.mined = None

    async def __call__(self, method, params):
        if method == "eth_blockNumber":
            self.calls.append((method, params))
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            self.calls.append((method, params))
            transactions = [self.transaction] if params[1] and int(params[0], 16) == self.mined else []
            return {"hash": "0x" + "11" * 32, "transactions": transactions}
        result = await super().__call__(method, params)
        if method == "eth_sendRawTransaction":
            self.nonce += 1
        return result

    def confirm(self, payment, status=1):
        self.mined = self.head
        tx = payment["transaction"]
        self.transaction = {
            **tx,
            "nonce": tx.get("nonce", hex(self.nonce)),
            "input": tx["data"],
            "hash": payment["tx_hash"],
        }
        self.receipt = {
            "transactionHash": payment["tx_hash"],
            "blockNumber": hex(self.mined),
            "blockHash": "0x" + "11" * 32,
            "status": hex(status),
        }
        self.head += 1


def setup(config, rows=None):
    rows = rows if rows is not None else []
    bridge = WalletNetwork(config, rows)
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    wallets = Wallets(config, store)
    chain = RabdiChain()
    wallets.rabdi.rpc_override = chain
    return bridge, store, wallets, chain


def checkpoint(bridge, store):
    if bridge.pending("u"):
        bridge.attach_external("u", "0x" + "ef" * 32)
        store.accept_checkpoint("u")


async def approved_checkpoints(awaitable, bridge, store):
    operation = asyncio.create_task(awaitable)
    try:
        for _ in range(80):
            if operation.done():
                return await operation
            checkpoint(bridge, store)
            await asyncio.sleep(0.03)
        return await asyncio.wait_for(operation, 1)
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


async def test_local_tglm_approval_uses_nonce_after_checkpoints_and_releases_storage_after_receipt(config):
    bridge, store, wallets, chain = setup(config)
    try:
        sender = wallets.create("u")
        task = store.create_task("u", "tGLM payment", "Send 0.001 tGLM", "Verified Rabdi receipt")
        payment = await wallets.rabdi.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        assert payment["asset"] == "tGLM" and payment["chain_id"] == RABDI
        assert "nonce" not in payment["transaction"]
        assert "0.001 tGLM on Arkiv Rabdi" in ChatCommands.review(payment)
        with pytest.raises(ValueError, match="explicit incoming"):
            await wallets.approve("u", payment["id"])
        chain.nonce = 9  # Storage used this wallet after the preview was prepared.
        count = len(bridge.rows)
        submitted = await approve(wallets, store, payment)
        assert submitted["transaction"]["nonce"] == "0x9"
        raw = wallets.load("u", payment["id"])["raw"]
        assert Account.recover_transaction(raw) == sender["address"]
        assert payment_reserved(config, sender["address"])
        assert len(bridge.rows) == count + 1  # Only the approved intent precedes admission.
        store.flush_task(task["id"], force=True)
        assert len(bridge.rows) == count + 1  # Storage cannot consume the reserved Rabdi nonce.
        await approve(wallets, store, payment)
        assert len([m for m, _ in chain.calls if m == "eth_sendRawTransaction"]) == 1
        chain.confirm(submitted)
        verified = await wallets.status("u", payment["id"])
        assert verified["status"] == "confirmed"
        assert verified["explorer"].startswith("https://rabdi.explorer.arkiv.network/tx/")
        assert not payment_reserved(config, sender["address"])
        assert len(bridge.rows) > count + 1
    finally:
        await wallets.close()
        store.close()


async def test_connected_wallet_funds_its_own_agent_wallet_and_never_pins_a_stale_nonce(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    bridge, store, wallets, chain = setup(config)
    try:
        destination = wallets.create("u")
        account, _ = connect(wallets)
        task = store.create_task("u", "Fund agent", "Send 0.001 tGLM to agent wallet", "Verified receipt")
        checkpoint(bridge, store)
        payment = await wallets.rabdi.prepare("u", task["id"], "", "0.001")
        checkpoint(bridge, store)
        assert payment["recipient"] == destination["address"]
        assert payment["sender"] == account.address
        assert wallets.get("u")["agent_wallet"]["address"] == destination["address"]
        approved = await approved_checkpoints(approve(wallets, store, payment), bridge, store)
        token = approved["confirmation_link"].split("/")[-1]
        chain.nonce += 12
        claim = await approved_checkpoints(wallets.mobile.claim(token), bridge, store)
        assert "nonce" not in claim["transaction"]
        assert int(claim["transaction"]["chainId"], 16) == RABDI
        with pytest.raises(ValueError, match="already started"):
            await wallets.mobile.claim(token)
        remote_count = len(bridge.rows)
        submitted = {**payment, "tx_hash": "0x" + "ab" * 32}
        chain.confirm(submitted)
        chain.transaction["chainId"] = hex(11155111)
        with pytest.raises(ValueError, match="does not yet identify"):
            await wallets.mobile.attach(token, submitted["tx_hash"])
        chain.transaction["chainId"] = hex(RABDI)
        await wallets.mobile.attach(token, submitted["tx_hash"])
        assert len(bridge.rows) == remote_count
        verified = await wallets.status("u", payment["id"])
        assert verified["status"] == "confirmed"
        assert bridge.pending("u")  # Receipt checkpoint is prepared only after the payment frees the wallet.
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
    finally:
        await wallets.close()
        store.close()


async def test_lost_local_send_response_and_cache_loss_recover_original_hash_without_resending(config):
    rows = []
    bridge, store, wallets, chain = setup(config, rows)
    try:
        wallets.create("u")
        task = store.create_task("u", "Pay once", "Send tGLM", "Receipt")
        payment = await wallets.rabdi.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        chain.send_failure = True
        unknown = await approve(wallets, store, payment)
        assert unknown["status"] == "unknown" and unknown["tx_hash"]
        # The latest remote snapshot predates signing; the admission journal has the original signed hash.
        remote = store.vault.open(
            rows[-1]["encrypted"], f"{rows[-1]['instance']}:{rows[-1]['task']}:{rows[-1]['revision']}"
        )
        assert remote["transfers"][0]["tx_hash"] is None
    finally:
        await wallets.close()
        store.close()
    config.db_path.unlink()
    bridge, store, wallets, _ = setup(config, rows)
    wallets.rabdi.rpc_override = chain
    try:
        wallets.rabdi.restore()
        recovered = wallets.load("u", payment["id"])
        assert recovered["tx_hash"] == unknown["tx_hash"] and recovered["raw"]
        chain.confirm(recovered)
        assert (await wallets.status("u", payment["id"]))["status"] == "confirmed"
        await approve(wallets, store, payment)
        assert len([m for m, _ in chain.calls if m == "eth_sendRawTransaction"]) == 1
    finally:
        await wallets.close()
        store.close()


async def test_metamask_lost_response_is_found_by_unique_payment_reference(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    bridge, store, wallets, chain = setup(config)
    try:
        wallets.create("u")
        connect(wallets)
        task = store.create_task("u", "Fund agent", "Send tGLM", "Receipt")
        checkpoint(bridge, store)
        payment = await wallets.rabdi.prepare("u", task["id"], "", "0.001")
        checkpoint(bridge, store)
        approved = await approved_checkpoints(approve(wallets, store, payment), bridge, store)
        token = approved["confirmation_link"].split("/")[-1]
        await approved_checkpoints(wallets.mobile.claim(token), bridge, store)
        submitted = {**payment, "tx_hash": "0x" + "cd" * 32}
        chain.confirm(submitted)
        # No callback or hash attachment: polling recognizes the exact reference, not just the amount.
        result = await wallets.status("u", payment["id"])
        assert result["tx_hash"] == submitted["tx_hash"] and result["status"] == "confirmed"
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
    finally:
        await wallets.close()
        store.close()


async def test_wrong_network_amount_budget_and_changed_intent_never_send(config):
    bridge, store, wallets, chain = setup(config)
    try:
        wallets.create("u")
        task = store.create_task("u", "Pay", "Send tGLM", "Receipt")
        chain.chain = 11155111
        with pytest.raises(ValueError, match="Wrong payment network"):
            await wallets.rabdi.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        chain.chain = RABDI
        with pytest.raises(ValueError, match="AGENT_MAX_TRANSFER_TGLM"):
            await wallets.rabdi.prepare("u", task["id"], "0x" + "12" * 20, "1")
        payment = await wallets.rabdi.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        with pytest.raises(ValueError, match="different payment intent"):
            await wallets.rabdi.prepare("u", task["id"], "0x" + "34" * 20, "0.001")
        changed = {**payment, "transaction": {**payment["transaction"], "data": "0x"}}
        tx = {**changed["transaction"], "input": "0x", "nonce": "0x1"}
        assert not wallets.rabdi.matches(payment, tx)
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
    finally:
        await wallets.close()
        store.close()


async def test_failure_to_persist_signed_bytes_never_admits_a_send(config, monkeypatch):
    bridge, store, wallets, chain = setup(config)
    try:
        wallets.create("u")
        task = store.create_task("u", "Pay", "Send tGLM", "Receipt")
        payment = await wallets.rabdi.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        original = wallets.rabdi.journal

        def failed(value, state):
            if state == "submitting":
                raise OSError("Disk full before send admission")
            return original(value, state)

        monkeypatch.setattr(wallets.rabdi, "journal", failed)
        with pytest.raises(OSError, match="before send admission"):
            await approve(wallets, store, payment)
        with pytest.raises(ValueError, match="already has an admitted"):
            await approve(wallets, store, payment)
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
    finally:
        await wallets.close()
        store.close()


async def test_whatsapp_prompt_task_review_and_raw_approval_use_rabdi_not_sepolia_or_ccip(config):
    from agent.providers import Answer
    from agent.runtime import Runtime
    from tests.test_agent import Script, call, last_result
    from tests.test_web3 import MemoryArkiv

    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    models = Script(
        call(
            "create_task",
            title="Fund agent wallet",
            instruction="Send 0.001 tGLM from connected wallet to agent",
            done_when="Verified Rabdi receipt",
        ),
        Answer("I will prepare the tGLM transfer for your approval."),
        call("prepare_rabdi_transfer", amount_tglm="0.001"),
        lambda _r, messages, _t: call("ask_user", question=last_result(messages)["output"]["review_message"]),
    )
    runtime = Runtime(config, models)
    runtime.store.close()
    runtime.store = ArkivStore(config.db_path, config, MemoryArkiv())
    runtime.engine.store = runtime.store
    runtime.wallets.store = runtime.store
    chain = RabdiChain()
    runtime.wallets.rabdi.rpc_override = chain
    try:
        destination = runtime.wallets.create("u")
        source, _ = connect(runtime.wallets)
        reply = await runtime.chat("u", "Give 0.001 tGLM from my wallet to the agent wallet")
        assert "approval" in reply
        assert await runtime.run_once()
        [payment] = runtime.wallets.list("u")
        assert payment["kind"] == "rabdi_native" and payment["asset"] == "tGLM"
        assert payment["sender"] == source.address and payment["recipient"] == destination["address"]
        assert payment["transaction"]["chainId"] == hex(RABDI)
        calls = len(models.calls)
        reply = await runtime.chat("u", "approve " + payment["approval_code"])
        assert "link.metamask.io" in reply and "tGLM" in reply
        assert len(models.calls) == calls  # Raw approval never delegates authority to the model.
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
    finally:
        await runtime.close()
