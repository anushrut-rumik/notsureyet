"""MetaMask Base Sepolia funding using the existing payment/approval/receipt flow."""

import pytest

from agent.chat_commands import ChatCommands
from agent.payments import prepare_request, refresh_review
from agent.wallet import BASE_SEPOLIA, Wallets
from tests.test_mobile_wallet import connect
from tests.test_mobile_wallet import connected as connected
from tests.test_web3 import Chain
from tests.test_web3 import config as config


async def draft(wallets):
    return await prepare_request(
        wallets,
        "u",
        asset="ETH",
        amount="0.005",
        source_network="base_sepolia",
        destination_network="base_sepolia",
    )


async def test_base_funding_approval_metamask_and_receipt_use_only_base_rpc(connected):
    _, store, _, sepolia, wallets = connected
    try:
        agent = wallets.create("u")
        sender, _ = connect(wallets)
        base = Chain()
        base.chain = BASE_SEPOLIA
        wallets.base_rpc = base
        payment = await draft(wallets)
        assert payment["sender"] == sender.address
        assert payment["recipient"] == agent["address"]
        assert payment["chain_id"] == BASE_SEPOLIA
        assert payment["transaction"]["chainId"] == "0x14a34"
        assert "Base Sepolia (84532)" in ChatCommands.review(payment)
        assert "L1 data fee" in ChatCommands.review(payment)
        assert (await draft(wallets))["id"] == payment["id"]
        with pytest.raises(ValueError, match="explicit incoming"):
            await wallets.approve("u", payment["id"])
        event = store.event(
            "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=payment["task_id"], source="user"
        )
        reviewed = await wallets.approve("u", payment["id"], event)
        token = reviewed["confirmation_link"].split("/")[-1]
        claimed = await wallets.mobile.claim(token)
        assert claimed["transaction"]["chainId"] == "0x14a34"
        with pytest.raises(ValueError, match="already started"):
            await wallets.mobile.claim(token)
        payment["tx_hash"] = "0x" + "ab" * 32
        base.confirm(payment)
        base.transaction["chainId"] = hex(11155111)
        with pytest.raises(ValueError, match="does not match"):
            await wallets.mobile.attach(token, payment["tx_hash"])
        base.confirm(payment)
        await wallets.mobile.attach(token, payment["tx_hash"])
        verified = await wallets.status("u", payment["id"])
        assert verified["status"] == "confirmed"
        assert verified["explorer"].startswith("https://sepolia.basescan.org/tx/")
        assert not sepolia.calls
        assert not any(method.startswith("eth_send") for method, _ in base.calls)
        assert (await wallets.approve("u", payment["id"]))["status"] == "confirmed"
    finally:
        await wallets.close()
        store.close()


async def test_base_funding_wrong_rpc_and_insufficient_balance_fail_before_draft(connected):
    _, store, _, _, wallets = connected
    try:
        wallets.create("u")
        connect(wallets)
        base = Chain()
        wallets.base_rpc = base
        with pytest.raises(ValueError, match="Wrong execution network"):
            await draft(wallets)
        base.chain = BASE_SEPOLIA
        base.balance = 0
        with pytest.raises(ValueError, match="Insufficient ETH"):
            await draft(wallets)
        assert not wallets.list("u")
    finally:
        await wallets.close()
        store.close()


async def test_base_refresh_keeps_chain_and_requires_fresh_approval(connected):
    _, store, _, sepolia, wallets = connected
    try:
        wallets.create("u")
        connect(wallets)
        base = Chain()
        base.chain = BASE_SEPOLIA
        wallets.base_rpc = base
        payment = await draft(wallets)
        updated = await refresh_review(wallets, "u", payment["id"])
        assert updated["approval_code"] != payment["approval_code"]
        assert updated["transaction"]["chainId"] == hex(BASE_SEPOLIA)
        assert not sepolia.calls
    finally:
        await wallets.close()
        store.close()


def test_base_transaction_cannot_match_a_sepolia_payment():
    payment = {
        "sender": "0x" + "11" * 20,
        "chain_id": 11155111,
        "value_wei": "1",
        "transaction": {"to": "0x" + "22" * 20, "nonce": "0x0", "data": "0x"},
    }
    tx = {
        "from": payment["sender"],
        "to": payment["transaction"]["to"],
        "nonce": "0x0",
        "value": "0x1",
        "chainId": hex(BASE_SEPOLIA),
        "input": "0x",
    }
    assert not Wallets.matches(payment, tx)


async def test_pending_sepolia_payment_does_not_block_base_nonce(connected):
    _, store, _, _, wallets = connected
    try:
        wallets.create("u")
        connect(wallets)
        base = Chain()
        base.chain = BASE_SEPOLIA
        wallets.base_rpc = base
        payment = await draft(wallets)
        old = await prepare_request(
            wallets,
            "u",
            asset="ETH",
            amount="0.001",
            source_network="ethereum_sepolia",
            destination_network="ethereum_sepolia",
        )
        old = wallets.load("u", old["id"])
        old["status"] = "awaiting_signature"
        wallets.save(old)
        event = store.event(
            "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=payment["task_id"], source="user"
        )
        assert (await wallets.approve("u", payment["id"], event))["status"] == "awaiting_signature"
    finally:
        await wallets.close()
        store.close()
