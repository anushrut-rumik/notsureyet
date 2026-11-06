"""Same-chain test-token transfers; no bridge, router or allowance transaction."""

from agent.balances import NETWORKS, read_token
from agent.ccip import TOKEN_TOPIC, calldata, token_units
from agent.wallet import address


def details(chain_id, recipient, amount):
    network = next((n for n, (chain, _) in NETWORKS.items() if chain == chain_id), None)
    if not network:
        raise ValueError("Unsupported token transfer network")
    recipient = address(recipient)
    units = token_units(amount)
    contract = NETWORKS[network][1]["CCIP-BnM"]
    return contract, units, calldata("transfer(address,uint256)", ["address", "uint256"], [recipient, units])


async def preflight(wallets, user, chain_id, recipient, amount):
    wallet = wallets.get(user)
    if wallet["kind"] != "metamask":
        raise ValueError("Connect MetaMask for this same-chain token transfer")
    contract, units, data = details(chain_id, recipient, amount)
    if units > token_units(wallets.config.env.get("AGENT_MAX_TRANSFER_BNM") or "1"):
        raise ValueError("Transfer exceeds AGENT_MAX_TRANSFER_BNM")
    await wallets.network(chain_id)
    rpc = wallets.rpc_for(chain_id)
    block = await rpc("eth_blockNumber", [])
    token = await read_token(rpc, wallet["address"], contract, block)
    if token["decimals"] != 18:
        raise ValueError("Unexpected CCIP-BnM decimals; transfer not prepared")
    if int(token["balance_units"]) < units:
        raise ValueError("Insufficient CCIP-BnM on the selected source network")
    if int(await rpc("eth_getBalance", [wallet["address"], block]), 16) == 0:
        raise ValueError("The MetaMask wallet needs test ETH on this network for fees")
    return contract, units, data


def receipt_matches(payment, receipt):
    """Success requires the actual token contract's exact Transfer event."""
    try:
        contract, units, data = details(payment["chain_id"], payment["recipient"], payment["amount"])
        if (
            payment["transaction"]["to"].lower() != contract.lower()
            or payment["transaction"]["data"].lower() != data.lower()
            or int(payment["value_wei"]) != 0
            or int(payment["amount_units"]) != units
        ):
            return False
        topics = [
            TOKEN_TOPIC.lower(),
            "0x" + payment["sender"][2:].lower().zfill(64),
            "0x" + payment["recipient"][2:].lower().zfill(64),
        ]
        matches = [
            log
            for log in receipt.get("logs", [])
            if log["address"].lower() == contract.lower()
            and [t.lower() for t in log["topics"]] == topics
            and len(log["data"]) == 66
            and int(log["data"], 16) == units
            and not log.get("removed")
            and log["transactionHash"].lower() == receipt["transactionHash"].lower()
            and log["blockHash"].lower() == receipt["blockHash"].lower()
        ]
        return len(matches) == 1
    except (KeyError, TypeError, ValueError):
        return False
