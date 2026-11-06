"""Public testnet balances, independent of signing, deployment and lending setup."""

import asyncio
from typing import Literal

from agent.wallet import BASE_SEPOLIA, SEPOLIA, address

# Official Chainlink CCIP Directory and LINK token-contract listings.
# https://docs.chain.link/ccip/directory/testnet/token/CCIP-BnM
# https://docs.chain.link/resources/link-token-contracts
NETWORKS = {
    "ethereum_sepolia": (
        SEPOLIA,
        {
            "CCIP-BnM": "0xFd57b4ddBf88a4e07fF4e34C487b99af2Fe82a05",
            "LINK": "0x779877A7B0D9E8603169DdbD7836e478b4624789",
        },
    ),
    "base_sepolia": (
        BASE_SEPOLIA,
        {
            "CCIP-BnM": "0x88A2d74F47a237a62e7A51cdDa67270CE381555e",
            "LINK": "0xE4aB69C077896252FAFBD49EFD26B5D171A32410",
        },
    ),
}


def units_decimal(units, decimals):
    whole, fraction = divmod(units, 10**decimals)
    return str(whole) + ("." + str(fraction).zfill(decimals).rstrip("0") if fraction else "")


def decode_uint(data):
    if not isinstance(data, str) or not data.startswith("0x") or len(data) != 66:
        raise ValueError("Token contract returned an invalid balance or decimals response")
    return int(data[2:], 16)


async def read_token(rpc, owner, contract, block):
    decimals, units = await asyncio.gather(
        rpc("eth_call", [{"to": contract, "data": "0x313ce567"}, block]),
        rpc("eth_call", [{"to": contract, "data": "0x70a08231" + owner[2:].lower().zfill(64)}, block]),
    )
    decimals, units = decode_uint(decimals), decode_uint(units)
    if decimals > 255:
        raise ValueError("Invalid token decimals")
    return {
        "status": "read",
        "contract": contract,
        "decimals": decimals,
        "balance_units": str(units),
        "balance": units_decimal(units, decimals),
    }


async def read_network(wallets, owner, name):
    chain_id, tokens = NETWORKS[name]
    rpc = wallets.rpc_for(chain_id)
    try:
        await wallets.network(chain_id)
        block = await rpc("eth_blockNumber", [])
        results = await asyncio.gather(
            rpc("eth_getBalance", [owner, block]),
            *(read_token(rpc, owner, contract, block) for contract in tokens.values()),
            return_exceptions=True,
        )
        assets = {}
        for symbol, result in zip(("ETH", *tokens), results, strict=True):
            if isinstance(result, Exception):
                assets[symbol] = {
                    "status": "unavailable",
                    "balance": None,
                    "error": "The network or token contract could not be read. This is not a zero balance.",
                }
            elif symbol == "ETH":
                units = int(result, 16)
                assets[symbol] = {
                    "status": "read",
                    "decimals": 18,
                    "balance_units": str(units),
                    "balance": units_decimal(units, 18),
                }
            else:
                assets[symbol] = result
        return {
            "network": name,
            "chain_id": chain_id,
            "block_number": int(block, 16),
            "status": "read" if all(a["status"] == "read" for a in assets.values()) else "partial",
            "assets": assets,
        }
    except Exception:
        return {
            "network": name,
            "chain_id": chain_id,
            "status": "unavailable",
            "assets": {},
            "error": "Could not verify this network and read its balances. Do not report missing balances as zero.",
        }


async def balances(wallets, user, *, wallet="connected", network="both", wallet_address=""):
    if wallet not in {"connected", "agent"} or network not in {"both", *NETWORKS}:
        raise ValueError("Choose connected or agent wallet, and Ethereum Sepolia, Base Sepolia or both")
    if wallet_address:
        owner, source = address(wallet_address), "public_address"
    elif wallet == "agent":
        owner, source = wallets.agent_wallet(user)["address"], "agent"
    else:
        connected = wallets.get(user)
        if connected["kind"] != "metamask":
            raise ValueError(
                "No connected MetaMask wallet. Select the agent wallet or supply a public address to read."
            )
        owner, source = connected["address"], "connected_metamask"
    names = list(NETWORKS) if network == "both" else [network]
    return {
        "address": owner,
        "wallet": source,
        "read_only": True,
        "networks": await asyncio.gather(*(read_network(wallets, owner, n) for n in names)),
    }


def register_balance_tools(registry):
    @registry.tool(evidence=True)
    async def wallet_balances(
        ctx,
        wallet: Literal["connected", "agent"] = "connected",
        network: Literal["both", "ethereum_sepolia", "base_sepolia"] = "both",
        wallet_address: str = "",
    ):
        """Read live ETH, CCIP-BnM and LINK balances on Ethereum Sepolia and Base Sepolia.
        Use wallet='connected' for the user's MetaMask, wallet='agent' for the encrypted agent wallet.
        A supplied public wallet_address overrides wallet selection. Both networks are read by default.
        This works even when lending/CCIP transfers are disabled or undeployed; no approval or signing is needed.
        Report unavailable reads as unknown, never as zero. Each network includes its observed block.
        """
        return await balances(
            ctx.engine.wallets, ctx.user_id, wallet=wallet, network=network, wallet_address=wallet_address
        )
