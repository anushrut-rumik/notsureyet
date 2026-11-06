"""Payment routes are validated before any storage request, preserving the exact user intent."""

import asyncio

import pytest

from agent.arkiv import ArkivStore
from agent.payments import prepare_request
from agent.runtime import Runtime
from agent.wallet import Wallets
from tests.test_agent import Script, call
from tests.test_arkiv_wallets import WalletNetwork
from tests.test_ccip import ccip as ccip
from tests.test_mobile_wallet import connect
from tests.test_web3 import Chain, MemoryArkiv
from tests.test_web3 import arkiv_store as arkiv_store
from tests.test_web3 import config as config

RECIPIENT = "0x" + "12" * 20


@pytest.fixture
async def payments(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    bridge = WalletNetwork(config, [])
    store = ArkivStore(config.db_path, config, bridge)
    wallets = Wallets(config, store, Chain())
    store.recover()
    connect(wallets)
    try:
        yield config, store, bridge, wallets
    finally:
        await wallets.close()
        store.close()


async def test_sepolia_review_precedes_storage_and_one_signature_does_not_reopen_checkpoint(payments):
    _, store, bridge, wallets = payments
    payment = await prepare_request(
        wallets,
        "u",
        asset="ETH",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="ethereum_sepolia",
        recipient=RECIPIENT,
    )
    assert payment["amount"] == "0.001" and payment["asset"] == "ETH" and payment["chain_id"] == 11155111
    assert int(payment["transaction"]["value"], 16) == 10**15
    assert not bridge.pending("u") and not bridge.rows
    assert store.claim() is None
    same = await prepare_request(
        wallets,
        "u",
        asset="ETH",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="ethereum_sepolia",
        recipient=RECIPIENT,
    )
    assert same["id"] == payment["id"]
    assert store.db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    event = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=payment["task_id"], source="user"
    )
    approval = asyncio.create_task(wallets.approve("u", payment["id"], event))
    try:
        await asyncio.sleep(0)
        assert not approval.done() and bridge.pending("u")
        bridge.attach_external("u", "0x" + "ab" * 32)
        approved = await asyncio.wait_for(approval, 2)
        assert approved["confirmation_link"]
        token = approved["confirmation_link"].split("/")[-1]
        before = len(bridge.rows)
        claimed = await wallets.mobile.claim(token)
        assert claimed["transaction"]["chainId"] == hex(11155111)
        assert claimed["transaction"]["value"] == hex(10**15)
        assert len(bridge.rows) == before and not bridge.pending("u")
        with pytest.raises(ValueError, match="already started"):
            await wallets.mobile.claim(token)
    finally:
        if not approval.done():
            approval.cancel()
            await asyncio.gather(approval, return_exceptions=True)


async def test_missing_ccip_deployment_is_reported_even_with_an_unrelated_pending_storage_request(payments):
    _, store, bridge, wallets = payments
    original = store.create_task("u", "Existing ETH funding", "Send 0.001 ETH on Sepolia", "Confirmed")
    pending = bridge.pending("u").copy()
    with pytest.raises(ValueError, match="AGENT_CCIP_SENDER_ADDRESS"):
        await prepare_request(
            wallets,
            "u",
            asset="CCIP-BnM",
            amount="0.001",
            source_network="ethereum_sepolia",
            destination_network="base_sepolia",
            recipient=RECIPIENT,
        )
    assert bridge.pending("u") == pending
    assert store.db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    assert store.task("u", original["id"])["title"] == "Existing ETH funding"
    assert not wallets.list("u")


@pytest.mark.parametrize(
    "asset,source,destination",
    [
        ("ETH", "ethereum_sepolia", "base_sepolia"),
        ("CCIP-BnM", "base_sepolia", "base_sepolia"),
        ("tGLM", "ethereum_sepolia", "arkiv_rabdi"),
    ],
)
async def test_wrong_route_never_silently_falls_back_to_rabdi(payments, asset, source, destination):
    _, store, bridge, wallets = payments
    with pytest.raises(ValueError, match="Unsupported payment route"):
        await prepare_request(
            wallets,
            "u",
            asset=asset,
            amount="0.001",
            source_network=source,
            destination_network=destination,
            recipient=RECIPIENT,
        )
    assert not bridge.pending("u")
    assert not wallets.list("u")
    assert store.db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


async def test_ccip_preparation_preserves_token_route_and_fee_currency(ccip):
    c = ccip
    before = len(c.bridge.rows)
    payment = await prepare_request(
        c.wallets,
        "u",
        asset="CCIP-BnM",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="base_sepolia",
        recipient=RECIPIENT,
        task_id=c.task["id"],
    )
    assert payment["kind"] == "ccip" and payment["asset"] == "CCIP-BnM"
    assert payment["amount_units"] == str(10**15)
    assert payment["destination"] == "Base Sepolia" and payment["chain_id"] == 11155111
    assert payment["ccip_fee_eth"] and len(c.bridge.rows) == before


async def test_metamask_claim_remains_one_shot_after_sqlite_recovery(payments):
    config, store, bridge, wallets = payments
    payment = await prepare_request(
        wallets,
        "u",
        asset="ETH",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="ethereum_sepolia",
        recipient=RECIPIENT,
    )
    event = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=payment["task_id"], source="user"
    )
    approval = asyncio.create_task(wallets.approve("u", payment["id"], event))
    await asyncio.sleep(0)
    bridge.attach_external("u", "0x" + "ab" * 32)
    approved = await asyncio.wait_for(approval, 2)
    token = approved["confirmation_link"].split("/")[-1]
    await wallets.mobile.claim(token)
    # Recovering older remote data must retain the durable, already-admitted local request.
    store.recover()
    wallets.restore()
    assert wallets.load("u", payment["id"])["signing_requested_at"]
    with pytest.raises(ValueError, match="already started"):
        await wallets.mobile.claim(token)
    assert not any(method.startswith("eth_send") for method, _ in wallets.rpc_override.calls)


async def test_chat_uses_exact_payment_review_without_creating_storage_task_first(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    models = Script(
        call(
            "prepare_payment",
            asset="ETH",
            amount="0.001",
            source_network="ethereum_sepolia",
            destination_network="ethereum_sepolia",
            recipient=RECIPIENT,
        )
    )
    runtime = Runtime(config, models)
    runtime.store.close()
    bridge = MemoryArkiv()
    runtime.store = ArkivStore(config.db_path, config, bridge)
    runtime.engine.store = runtime.wallets.store = runtime.wallets.ccip.store = runtime.store
    runtime.wallets.rpc_override = Chain()
    try:
        connect(runtime.wallets)
        reply = await runtime.chat("u", f"Send 0.001 ETH on Sepolia to {RECIPIENT}")
        assert "0.001 ETH on Ethereum Sepolia" in reply and RECIPIENT in reply
        assert "approve " in reply and "0 tGLM" not in reply
        assert not bridge.rows
        assert len(runtime.wallets.list("u")) == 1
        assert not await runtime.run_once()
    finally:
        await runtime.close()


async def test_chat_reports_missing_deployment_without_returning_old_storage_link(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    models = Script(
        call(
            "prepare_payment",
            asset="CCIP-BnM",
            amount="0.001",
            source_network="ethereum_sepolia",
            destination_network="base_sepolia",
            recipient=RECIPIENT,
        )
    )
    runtime = Runtime(config, models)
    runtime.store.close()
    bridge = MemoryArkiv()
    runtime.store = ArkivStore(config.db_path, config, bridge)
    runtime.engine.store = runtime.wallets.store = runtime.wallets.ccip.store = runtime.store
    try:
        connect(runtime.wallets)
        reply = await runtime.chat("u", "Send 0.001 CCIP-BnM from Ethereum Sepolia to Base Sepolia")
        assert "AGENT_CCIP_SENDER_ADDRESS" in reply
        assert "metamask.io" not in reply and "approve " not in reply
        assert not bridge.rows and not runtime.store.tasks("u")
    finally:
        await runtime.close()


async def test_expired_review_refreshes_same_id_and_rejects_old_approval(payments):
    _, store, bridge, wallets = payments
    payment = await prepare_request(
        wallets,
        "u",
        asset="ETH",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="ethereum_sepolia",
        recipient=RECIPIENT,
    )
    old_code = payment["approval_code"]
    payment = wallets.load("u", payment["id"])
    payment["expires_at"] = 0
    wallets.save(payment, checkpoint=False)
    refreshed = await prepare_request(
        wallets,
        "u",
        asset="ETH",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="ethereum_sepolia",
        recipient=RECIPIENT,
    )
    assert refreshed["id"] == payment["id"] and refreshed["task_id"] == payment["task_id"]
    assert refreshed["approval_code"] != old_code and refreshed["status"] == "awaiting_approval"
    assert not bridge.rows and not bridge.pending("u")
    event = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=payment["task_id"], source="user"
    )
    with pytest.raises(ValueError, match="exact transfer"):
        await wallets.approve("u", payment["id"], event)


async def test_payment_tool_cannot_be_preceded_by_storage_creation_in_same_model_response(config):
    from agent.providers import Answer

    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    calls = [
        call("create_task", title="Wrong generic payment task", instruction="Send ETH", done_when="Paid").calls[0],
        call(
            "prepare_payment",
            asset="CCIP-BnM",
            amount="0.001",
            source_network="ethereum_sepolia",
            destination_network="base_sepolia",
            recipient=RECIPIENT,
        ).calls[0],
    ]
    runtime = Runtime(config, Script(Answer(calls=calls)))
    runtime.store.close()
    bridge = MemoryArkiv()
    runtime.store = ArkivStore(config.db_path, config, bridge)
    runtime.engine.store = runtime.wallets.store = runtime.wallets.ccip.store = runtime.store
    try:
        connect(runtime.wallets)
        reply = await runtime.chat("u", "Send 0.001 CCIP-BnM from Sepolia to Base Sepolia")
        assert "AGENT_CCIP_SENDER_ADDRESS" in reply
        assert not bridge.rows and not runtime.store.tasks("u")
    finally:
        await runtime.close()


async def test_unsigned_local_review_survives_task_recovery_without_requesting_storage(payments):
    _, store, bridge, wallets = payments
    payment = await prepare_request(
        wallets,
        "u",
        asset="ETH",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="ethereum_sepolia",
        recipient=RECIPIENT,
    )
    assert not bridge.rows
    store.recover()  # The remote authority does not contain this unsigned draft yet.
    wallets.restore()
    assert wallets.load("u", payment["id"])["approval_code"] == payment["approval_code"]
    assert store.task("u", payment["task_id"])["status"] == "needs_input"
    store.maintain()  # Maintenance cannot turn an unsigned draft into a storage approval.
    assert not bridge.pending("u") and not bridge.rows
    assert store.claim() is None
