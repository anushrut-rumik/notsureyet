import copy

import pytest
from eth_abi import decode, encode

from agent.metamask_receipts import DELEGATION, IMPLEMENTATION, MANAGER, REDEEMED, matches_execution
from tests.test_base_funding import draft
from tests.test_mobile_wallet import connect
from tests.test_mobile_wallet import connected as connected
from tests.test_web3 import Chain
from tests.test_web3 import config as config

HASH = "0x" + "ab" * 32
BLOCK_HASH = "0x" + "11" * 32


def delegated(payment):
    relayer = "0x" + "22" * 20
    delegation = (relayer, payment["sender"], bytes.fromhex("ff" * 32), (), 1, b"signed")
    context = encode([DELEGATION + "[]"], [[delegation]])
    execution = bytes.fromhex(payment["recipient"][2:]) + int(payment["value_wei"]).to_bytes(32, "big")
    tx = {
        "hash": HASH,
        "from": relayer,
        "to": MANAGER,
        "value": "0x0",
        "nonce": "0x77",
        "chainId": hex(84532),
        "input": "0xcef6d209"
        + encode(["bytes[]", "bytes32[]", "bytes[]"], [[context], [bytes(32)], [execution]]).hex(),
    }
    receipt = {
        "status": "0x1",
        "transactionHash": HASH,
        "blockHash": BLOCK_HASH,
        "blockNumber": hex(120),
        "logs": [
            {
                "address": MANAGER,
                "topics": [REDEEMED, "0x" + payment["sender"][2:].lower().zfill(64), "0x" + relayer[2:].zfill(64)],
                "data": "0x" + encode([DELEGATION], [delegation]).hex(),
                "transactionHash": HASH,
                "blockHash": BLOCK_HASH,
                "removed": False,
            }
        ],
    }
    return tx, receipt


@pytest.fixture
async def claimed(connected):
    _, store, _, _, wallets = connected
    wallets.create("u")
    connect(wallets)
    base = Chain()
    base.chain = 84532
    wallets.base_rpc = base
    p = await draft(wallets)
    event = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(p)}, task_id=p["task_id"], source="user"
    )
    approved = await wallets.approve("u", p["id"], event)
    token = approved["confirmation_link"].split("/")[-1]
    await wallets.mobile.claim(token)
    p = wallets.load("u", p["id"])
    tx, receipt = delegated(p)

    async def rpc(method, params):
        base.calls.append((method, params))
        if method == "eth_getCode" and params[0].lower() == p["sender"].lower():
            return "0xef0100" + IMPLEMENTATION[2:]
        if method == "eth_blockNumber":
            return hex(1100)
        if method == "eth_getLogs":
            return receipt["logs"]
        if method == "eth_getTransactionByHash":
            return tx
        if method == "eth_getTransactionReceipt":
            return receipt
        return await base(method, params)

    wallets.base_rpc = rpc
    try:
        yield wallets, store, base, p, tx, receipt, token
    finally:
        await wallets.close()
        store.close()


async def test_recovers_successful_sponsored_payment_after_old_500_block_cutoff(claimed):
    wallets, _, base, p, _, _, _ = claimed
    result = await wallets.status("u", p["id"])
    assert result["status"] == "confirmed"
    assert result["tx_hash"] == HASH and result["receipt_kind"] == "metamask_delegation"
    assert any(m == "eth_getLogs" for m, _ in base.calls)
    for method, params in base.calls:
        if method == "eth_getLogs":
            assert int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) < 200
    assert not any(m.startswith("eth_send") for m, _ in base.calls)
    assert (await wallets.status("u", p["id"]))["status"] == "confirmed"


async def test_browser_callback_accepts_verified_delegation(claimed):
    wallets, _, _, p, _, _, token = claimed
    assert (await wallets.mobile.attach(token, HASH))["saved"]
    assert (await wallets.status("u", p["id"]))["status"] == "confirmed"


@pytest.mark.parametrize(
    "change", ["value", "recipient", "sender", "chain", "reverted", "old", "event", "manager", "malformed"]
)
async def test_mismatched_delegated_payments_are_rejected(claimed, change):
    _, _, _, p, tx, receipt, _ = claimed
    p, tx, receipt = copy.deepcopy((p, tx, receipt))
    if change == "value":
        p["value_wei"] = "1"
    if change == "recipient":
        p["recipient"] = "0x" + "33" * 20
    if change == "sender":
        p["sender"] = "0x" + "33" * 20
    if change == "chain":
        tx["chainId"] = hex(11155111)
    if change == "reverted":
        receipt["status"] = "0x0"
    if change == "old":
        receipt["blockNumber"] = "0x1"
    if change == "event":
        receipt["logs"] = []
    if change == "manager":
        tx["to"] = "0x" + "33" * 20
    if change == "malformed":
        tx["input"] = "0xcef6d209ff"
    assert not matches_execution(p, tx, receipt)


async def test_reorg_and_wrong_account_implementation_do_not_confirm(claimed):
    wallets, _, _, p, tx, receipt, token = claimed
    receipt["blockHash"] = "0x" + "99" * 32
    receipt["logs"][0]["blockHash"] = receipt["blockHash"]
    with pytest.raises(ValueError, match="does not match"):
        await wallets.mobile.attach(token, HASH)
    receipt["blockHash"] = receipt["logs"][0]["blockHash"] = BLOCK_HASH
    original = wallets.base_rpc

    async def rpc(method, params):
        if method == "eth_getCode":
            return "0xef0100" + "99" * 20
        return await original(method, params)

    wallets.base_rpc = rpc
    with pytest.raises(ValueError, match="does not match"):
        await wallets.mobile.attach(token, HASH)
    assert wallets.load("u", p["id"])["tx_hash"] is None


async def test_same_hash_cannot_complete_two_payments(claimed):
    wallets, store, _, p, _, _, token = claimed
    other = {**p, "id": "f" * 32, "tx_hash": HASH}
    # An independent previously recorded payment must never be credited again.
    import json

    store.db.execute(
        "INSERT INTO transfers(id,user_id,task_id,data) VALUES(?,?,?,?)",
        (other["id"], "other", "another-task", json.dumps(other)),
    )
    store.db.commit()
    with pytest.raises(ValueError, match="already assigned"):
        await wallets.mobile.attach(token, HASH)


@pytest.mark.parametrize("change", ["batch", "non_reverting", "calldata", "removed_event"])
async def test_unsupported_executions_cannot_be_treated_as_native_payment(claimed, change):
    _, _, _, p, tx, receipt, _ = claimed
    contexts, modes, executions = decode(["bytes[]", "bytes32[]", "bytes[]"], bytes.fromhex(tx["input"][10:]))
    if change == "batch":
        contexts, modes, executions = contexts * 2, modes * 2, executions * 2
    if change == "non_reverting":
        modes = (bytes.fromhex("0001" + "00" * 30),)
    if change == "calldata":
        executions = (executions[0] + b"other action",)
    if change == "removed_event":
        receipt["logs"][0]["removed"] = True
    tx["input"] = "0xcef6d209" + encode(["bytes[]", "bytes32[]", "bytes[]"], [contexts, modes, executions]).hex()
    assert not matches_execution(p, tx, receipt)
