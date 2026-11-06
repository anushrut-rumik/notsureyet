import copy

import pytest
from eth_abi import encode

from agent.balances import NETWORKS
from agent.ccip import TOKEN_TOPIC
from agent.chat_commands import ChatCommands
from agent.metamask_receipts import DELEGATION, IMPLEMENTATION, MANAGER, REDEEMED
from agent.payments import prepare_request, refresh_review
from agent.task_state import public_state
from agent.token_transfer import receipt_matches
from tests.test_mobile_wallet import connect
from tests.test_mobile_wallet import connected as connected
from tests.test_web3 import Chain
from tests.test_web3 import config as config

HASH = "0x" + "ab" * 32
BLOCK = "0x" + "11" * 32


class TokenChain(Chain):
    def __init__(self):
        super().__init__()
        self.chain = 84532
        self.token_balance = 8 * 10**17
        self.smart = False
        self.sender = ""

    async def __call__(self, method, params):
        if method == "eth_call":
            self.calls.append((method, params))
            value = 18 if params[0]["data"] == "0x313ce567" else self.token_balance
            return "0x" + value.to_bytes(32, "big").hex()
        if method == "eth_getCode" and self.smart and params[0].lower() == self.sender.lower():
            return "0xef0100" + IMPLEMENTATION[2:]
        if method == "eth_getLogs":
            return self.receipt["logs"]
        return await super().__call__(method, params)


@pytest.fixture
async def token_wallet(connected):
    config, store, _, sepolia, wallets = connected
    agent = wallets.create("u")
    sender, _ = connect(wallets)
    base = TokenChain()
    base.sender = sender.address
    wallets.base_rpc = base
    # A local cross-chain simulator must never capture this real same-chain route.
    config.env["AGENT_CCIP_MODE"] = "simulation"
    try:
        yield wallets, store, base, sepolia, agent
    finally:
        await wallets.close()
        store.close()


async def draft(wallets, amount="0.2"):
    return await prepare_request(
        wallets, "u", asset="CCIP-BnM", amount=amount, source_network="base_sepolia", destination_network="base_sepolia"
    )


async def admit(wallets, store, payment):
    event = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=payment["task_id"], source="user"
    )
    reviewed = await wallets.approve("u", payment["id"], event)
    token = reviewed["confirmation_link"].split("/")[-1]
    await wallets.mobile.claim(token)
    return token


def confirm(base, p):
    base.confirm({**p, "tx_hash": HASH})
    base.transaction["input"] = p["transaction"]["data"]
    base.receipt["blockNumber"] = "0x65"
    base.receipt["logs"] = [
        {
            "address": p["token_address"],
            "topics": [
                TOKEN_TOPIC,
                "0x" + p["sender"][2:].lower().zfill(64),
                "0x" + p["recipient"][2:].lower().zfill(64),
            ],
            "data": "0x" + int(p["amount_units"]).to_bytes(32, "big").hex(),
            "transactionHash": HASH,
            "blockHash": BLOCK,
            "removed": False,
        }
    ]


async def test_same_chain_token_approval_claim_and_verified_transfer(token_wallet):
    wallets, store, base, sepolia, agent = token_wallet
    p = await draft(wallets)
    assert p["kind"] == "erc20_transfer" and p["recipient"] == agent["address"]
    assert p["transaction"]["to"] == NETWORKS["base_sepolia"][1]["CCIP-BnM"]
    assert p["transaction"]["data"].startswith("0xa9059cbb")
    assert p["transaction"]["value"] == "0x0" and p["amount_units"] == "200000000000000000"
    assert "ETH\n" in ChatCommands.review(p) and "Same-chain token transfer" in ChatCommands.review(p)
    assert (await draft(wallets))["id"] == p["id"]
    with pytest.raises(ValueError, match="explicit incoming"):
        await wallets.approve("u", p["id"])
    token = await admit(wallets, store, p)
    page = wallets.mobile.info(token)["payment"]
    assert page["id"] == p["id"] and page["amount"] == "0.2" and page["asset"] == "CCIP-BnM"
    assert page["token_address"] == p["transaction"]["to"] and page["fee_asset"] == "ETH"
    confirm(base, p)
    # Two confirmations at block 102.
    old = wallets.base_rpc

    async def rpc(m, params):
        if m == "eth_blockNumber":
            return "0x66"
        return await old(m, params)

    wallets.base_rpc = rpc
    await wallets.mobile.attach(token, HASH)
    result = await wallets.status("u", p["id"])
    assert result["status"] == "confirmed"
    assert (await wallets.approve("u", p["id"]))["status"] == "confirmed"
    assert not sepolia.calls
    assert not any(m.startswith("eth_send") for m, _ in base.calls)
    state = public_state({"task": {"status": "needs_input"}, "transfers": [p]})
    assert state["source_network"] == state["destination_network"] == "base_sepolia"
    assert state["protocol"] == "erc20"


async def test_old_eth_receipt_and_new_token_link_keep_separate_payment_details(token_wallet):
    wallets, store, base, _, _ = token_wallet
    old = await prepare_request(
        wallets, "u", asset="ETH", amount="0.005", source_network="base_sepolia",
        destination_network="base_sepolia",
    )
    old_token = await admit(wallets, store, old)
    saved = wallets.load("u", old["id"])
    saved.update(status="confirmed", tx_hash=HASH)
    wallets.save(saved)
    new = await draft(wallets)
    event = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(new)}, task_id=new["task_id"], source="user"
    )
    approved = await wallets.approve("u", new["id"], event)
    new_token = approved["confirmation_link"].split("/")[-1]
    assert approved["review_link"].split("/")[-1] == new_token
    old_page = wallets.mobile.info(old_token)["payment"]
    new_page = wallets.mobile.info(new_token)["payment"]
    assert (old_page["id"], old_page["amount"], old_page["asset"], old_page["status"]) == (
        old["id"], "0.005", "ETH", "confirmed"
    )
    assert (new_page["id"], new_page["amount"], new_page["asset"]) == (new["id"], "0.2", "CCIP-BnM")
    with pytest.raises(ValueError, match="already started"):
        await wallets.mobile.claim(old_token)
    request = await wallets.mobile.claim(new_token)
    assert request["transfer_id"] == new["id"]
    assert request["transaction"]["value"] == "0x0"
    assert int(request["transaction"]["data"][-64:], 16) == 2 * 10**17


async def test_token_and_eth_balance_checks_and_refresh(token_wallet):
    wallets, _, base, _, _ = token_wallet
    base.token_balance = 0
    with pytest.raises(ValueError, match="Insufficient CCIP-BnM"):
        await draft(wallets)
    assert not wallets.list("u")
    base.token_balance = 8 * 10**17
    p = await draft(wallets)
    fresh = await refresh_review(wallets, "u", p["id"])
    assert fresh["approval_code"] != p["approval_code"]
    assert fresh["transaction"]["data"] == p["transaction"]["data"]
    base.balance = 0
    with pytest.raises(ValueError, match="test ETH"):
        await refresh_review(wallets, "u", p["id"])


async def test_success_without_exact_transfer_event_is_not_confirmation(token_wallet):
    wallets, store, base, _, _ = token_wallet
    p = await draft(wallets)
    await admit(wallets, store, p)
    confirm(base, p)
    assert receipt_matches(p, base.receipt)
    for change in ["amount", "recipient", "contract", "removed"]:
        receipt = copy.deepcopy(base.receipt)
        log = receipt["logs"][0]
        if change == "amount":
            log["data"] = "0x" + "00" * 32
        if change == "recipient":
            log["topics"][2] = "0x" + "33" * 32
        if change == "contract":
            log["address"] = "0x" + "33" * 20
        if change == "removed":
            log["removed"] = True
        assert not receipt_matches(p, receipt)


async def test_sponsored_token_transfer_is_recovered_and_verified(token_wallet):
    wallets, store, base, _, _ = token_wallet
    p = await draft(wallets)
    await admit(wallets, store, p)
    p = wallets.load("u", p["id"])
    confirm(base, p)
    relayer = "0x" + "22" * 20
    delegation = (relayer, p["sender"], bytes.fromhex("ff" * 32), (), 1, b"signed")
    context = encode([DELEGATION + "[]"], [[delegation]])
    execution = bytes.fromhex(p["transaction"]["to"][2:]) + bytes(32) + bytes.fromhex(p["transaction"]["data"][2:])
    base.transaction = {
        "hash": HASH,
        "from": relayer,
        "to": MANAGER,
        "value": "0x0",
        "nonce": "0x9",
        "chainId": hex(84532),
        "input": "0xcef6d209"
        + encode(["bytes[]", "bytes32[]", "bytes[]"], [[context], [bytes(32)], [execution]]).hex(),
    }
    base.receipt["logs"].append(
        {
            "address": MANAGER,
            "topics": [REDEEMED, "0x" + p["sender"][2:].lower().zfill(64), "0x" + relayer[2:].zfill(64)],
            "data": "0x" + encode([DELEGATION], [delegation]).hex(),
            "transactionHash": HASH,
            "blockHash": BLOCK,
            "removed": False,
        }
    )
    base.smart = True
    old = wallets.base_rpc

    async def rpc(m, params):
        if m == "eth_blockNumber":
            return hex(1000)
        return await old(m, params)

    wallets.base_rpc = rpc
    result = await wallets.status("u", p["id"])
    assert result["status"] == "confirmed" and result["receipt_kind"] == "metamask_delegation"
    assert not any(m.startswith("eth_send") for m, _ in base.calls)
