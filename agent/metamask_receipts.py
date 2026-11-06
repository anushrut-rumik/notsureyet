"""Read-only verification of MetaMask's single native transfer delegation on Base Sepolia.

The outer transaction belongs to a relayer. Bind the successful execution to the
reviewed sender, recipient and value instead of using the relayer's nonce.
Unsupported batches, execution modes and account implementations fail closed.
"""

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError, EncodingError
from web3 import Web3

MANAGER = "0xdb9b1e94b5b69df7e401ddbede43491141047db3"
IMPLEMENTATION = "0x63c0c19a282a1b52b07dd5a65b58948a07dae32b"
DELEGATION = "(address,address,bytes32,(address,bytes,bytes)[],uint256,bytes)"
REDEEMED = Web3.keccak(text=f"RedeemedDelegation(address,address,{DELEGATION})").to_0x_hex()
SELECTOR = "0xcef6d209"


def eligible(payment):
    return (
        payment.get("chain_id") == 84532
        and (
            (
                payment.get("asset") == "ETH"
                and not payment.get("kind")
                and payment["transaction"].get("data", "0x") == "0x"
            )
            or (payment.get("asset") == "CCIP-BnM" and payment.get("kind") == "erc20_transfer")
        )
        and payment.get("wallet_kind") == "metamask"
        and bool(payment.get("signing_requested_at"))
    )


def matches_execution(payment, tx, receipt):
    """Require one exact, reverting-on-failure execution and its emitted delegation."""
    if not eligible(payment) or not receipt:
        return False
    try:
        if (
            (tx.get("to") or "").lower() != MANAGER
            or int(tx.get("chainId", "0x0"), 16) != 84532
            or int(tx["value"], 16) != 0
            or int(receipt["status"], 16) != 1
            or receipt["transactionHash"].lower() != tx["hash"].lower()
            or int(receipt["blockNumber"], 16) < payment["start_block"]
        ):
            return False
        calldata = tx.get("input", "")
        if calldata[:10].lower() != SELECTOR:
            return False
        contexts, modes, executions = decode(["bytes[]", "bytes32[]", "bytes[]"], bytes.fromhex(calldata[10:]))
        if not (len(contexts) == len(modes) == len(executions) == 1) or modes[0] != bytes(32):
            return False
        target = payment["transaction"]["to"] if payment.get("kind") == "erc20_transfer" else payment["recipient"]
        expected = (
            bytes.fromhex(target[2:])
            + int(payment["value_wei"]).to_bytes(32, "big")
            + bytes.fromhex(payment["transaction"].get("data", "0x")[2:])
        )
        if executions[0] != expected:
            return False
        delegations = decode([DELEGATION + "[]"], contexts[0])[0]
        if len(delegations) != 1 or delegations[0][1].lower() != payment["sender"].lower():
            return False
        sender_topic = "0x" + payment["sender"][2:].lower().zfill(64)
        relayer_topic = "0x" + tx["from"][2:].lower().zfill(64)
        event_data = "0x" + encode([DELEGATION], [delegations[0]]).hex()
        events = [
            log
            for log in receipt["logs"]
            if log["address"].lower() == MANAGER
            and [t.lower() for t in log["topics"]] == [REDEEMED, sender_topic, relayer_topic]
            and log["data"].lower() == event_data
            and not log.get("removed")
            and log["transactionHash"].lower() == tx["hash"].lower()
            and log["blockHash"].lower() == receipt["blockHash"].lower()
        ]
        if payment.get("kind") == "erc20_transfer":
            from agent.token_transfer import receipt_matches

            return len(events) == 1 and receipt_matches(payment, receipt)
        return len(events) == 1
    except (ValueError, KeyError, TypeError, OverflowError, DecodingError, EncodingError):
        return False


async def verify(wallets, payment, tx):
    if not eligible(payment):
        return False
    rpc = wallets.rpc_for(payment["chain_id"])
    receipt = await rpc("eth_getTransactionReceipt", [tx["hash"]])
    if not matches_execution(payment, tx, receipt):
        return False
    block = await rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
    if not block or block["hash"].lower() != receipt["blockHash"].lower():
        return False
    code = await rpc("eth_getCode", [payment["sender"], receipt["blockNumber"]])
    if code.lower() != "0xef0100" + IMPLEMENTATION[2:]:
        return False
    used = wallets.store.db.execute(
        "SELECT id FROM transfers WHERE id!=? AND json_extract(data,'$.chain_id')=84532 "
        "AND lower(json_extract(data,'$.tx_hash'))=?",
        (payment["id"], tx["hash"].lower()),
    ).fetchone()
    if used:
        raise ValueError("This transaction is already assigned to another payment; no second completion allowed")
    return True


async def recover(wallets, payment, head):
    """Find a lost browser callback by indexed delegation events, not a block scan."""
    if not eligible(payment):
        return False
    rpc = wallets.rpc_for(payment["chain_id"])
    code = await rpc("eth_getCode", [payment["sender"], hex(head)])
    if code.lower() != "0xef0100" + IMPLEMENTATION[2:]:
        return False
    start = max(payment["start_block"], payment.get("smart_scan_block", payment["start_block"]) - 64)
    # The public Base Sepolia RPC limits eth_getLogs to 200 blocks.
    end = min(head, start + 199)
    if end < start:
        return True
    logs = await rpc(
        "eth_getLogs",
        [
            {
                "address": MANAGER,
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "topics": [REDEEMED, "0x" + payment["sender"][2:].lower().zfill(64)],
            }
        ],
    )
    matches = []
    for tx_hash in dict.fromkeys(log["transactionHash"] for log in logs if not log.get("removed")):
        tx = await rpc("eth_getTransactionByHash", [tx_hash])
        if tx and await verify(wallets, payment, tx):
            matches.append(tx_hash)
    if len(matches) > 1:
        raise ValueError("Multiple matching smart-account transfers found; inspect their hashes without sending again")
    if matches:
        payment.update(tx_hash=matches[0], status="submitted", receipt_kind="metamask_delegation")
        wallets.save(payment)
    else:
        payment["smart_scan_block"] = end + 1
        wallets.save(payment, checkpoint=False)
    return True
