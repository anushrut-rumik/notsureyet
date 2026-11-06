from types import SimpleNamespace

import pytest

from agent.balances import NETWORKS, balances, register_balance_tools
from agent.tools import Context, Registry
from tests.test_mobile_wallet import connect
from tests.test_mobile_wallet import connected as connected
from tests.test_web3 import config as config


class ReadChain:
    def __init__(self, chain_id):
        self.chain_id = chain_id
        self.calls = []
        self.fail_contract = None
        self.zero = False

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getBalance":
            return hex(5 * 10**15)
        if method == "eth_call":
            if params[0]["to"] == self.fail_contract:
                raise ValueError("Provider unavailable")
            value = 18 if params[0]["data"] == "0x313ce567" else 0 if self.zero else 10**18 + 1
            return "0x" + value.to_bytes(32, "big").hex()
        raise AssertionError("Unexpected RPC method: " + method)


@pytest.fixture
async def reader(connected):
    _, store, _, _, wallets = connected
    agent = wallets.create("u")
    account, _ = connect(wallets)
    sepolia, base = ReadChain(11155111), ReadChain(84532)
    wallets.rpc_override, wallets.base_rpc = sepolia, base
    try:
        yield wallets, store, account.address, agent["address"], sepolia, base
    finally:
        await wallets.close()
        store.close()


async def test_reads_both_networks_and_preserves_exact_token_units(reader):
    wallets, _, meta, _, sepolia, base = reader
    result = await balances(wallets, "u")
    assert result["address"] == meta and result["wallet"] == "connected_metamask"
    assert len(result["networks"]) == 2
    for n in result["networks"]:
        assert n["status"] == "read" and n["block_number"] == 100
        assert n["assets"]["ETH"]["balance"] == "0.005"
        assert n["assets"]["CCIP-BnM"]["balance"] == "1.000000000000000001"
        assert n["assets"]["LINK"]["balance_units"] == "1000000000000000001"
    for rpc in (sepolia, base):
        for method, params in rpc.calls:
            assert not method.startswith("eth_send")
            if method in {"eth_call", "eth_getBalance"}:
                assert params[-1] == "0x64"


async def test_agent_selection_does_not_use_connected_metamask(reader):
    wallets, _, meta, agent, _, base = reader
    result = await balances(wallets, "u", wallet="agent", network="base_sepolia")
    assert result["address"] == agent and agent != meta
    calls = [p[0]["data"] for m, p in base.calls if m == "eth_call" and p[0]["data"].startswith("0x70a08231")]
    assert all(data.endswith(agent[2:].lower().zfill(64)) for data in calls)


async def test_public_address_reads_need_no_connected_wallet_or_deployment(reader):
    wallets, _, _, _, _, _ = reader
    target = "0x" + "12" * 20
    result = await balances(wallets, "unregistered-user", wallet_address=target)
    assert result["address"] == target and result["wallet"] == "public_address"
    assert all(n["status"] == "read" for n in result["networks"])


async def test_failed_token_read_is_unknown_while_other_balances_remain_available(reader):
    wallets, _, _, _, _, base = reader
    base.fail_contract = NETWORKS["base_sepolia"][1]["CCIP-BnM"]
    result = await balances(wallets, "u")
    a, b = result["networks"]
    assert a["status"] == "read" and b["status"] == "partial"
    assert b["assets"]["CCIP-BnM"]["balance"] is None
    assert b["assets"]["CCIP-BnM"]["status"] == "unavailable"
    assert b["assets"]["LINK"]["status"] == "read"


async def test_wrong_chain_cannot_produce_a_balance(reader):
    wallets, _, _, _, _, base = reader
    base.chain_id = 1
    result = await balances(wallets, "u", network="base_sepolia")
    assert result["networks"][0]["status"] == "unavailable"
    assert base.calls == [("eth_chainId", [])]


async def test_zero_balance_is_distinct_from_failure_and_tool_is_chat_evidence(reader):
    wallets, store, _, _, _, base = reader
    base.zero = True
    registry = Registry()
    register_balance_tools(registry)
    assert any(t["function"]["name"] == "wallet_balances" for t in registry.schemas("chat"))
    ctx = Context(SimpleNamespace(wallets=wallets, store=store), "u", "chat:u", "turn:test", 1)
    result = await registry.execute(
        ctx, {"function": {"name": "wallet_balances", "arguments": '{"network":"base_sepolia"}'}}
    )
    assert result["ok"] and result["evidence"]
    assert result["output"]["networks"][0]["assets"]["CCIP-BnM"]["balance"] == "0"
    assert not wallets.list("u")


async def test_missing_metamask_or_invalid_selection_does_not_guess(reader):
    wallets, _, _, _, _, _ = reader
    with pytest.raises(ValueError):
        await balances(wallets, "u", network="mainnet")
    with pytest.raises(ValueError):
        await balances(wallets, "u", wallet_address="not-an-address")
    with pytest.raises(ValueError):
        await balances(wallets, "unknown-user")


async def test_chat_can_read_tokens_with_lending_off_and_no_deployed_sender(config):
    from agent.providers import Answer
    from agent.runtime import Runtime
    from tests.test_agent import Script, call

    models = Script(call("wallet_balances", wallet="agent", network="both"), Answer("Balances checked."))
    runtime = Runtime(config, models)
    runtime.wallets.create("u")
    runtime.wallets.rpc_override = ReadChain(11155111)
    runtime.wallets.base_rpc = ReadChain(84532)
    try:
        assert runtime.lending.mode == "off"
        assert "AGENT_CCIP_SENDER_ADDRESS" not in config.env
        assert "Balances checked." in await runtime.chat("u", "Check my agent wallet CCIP-BnM and LINK balances")
        results = runtime.store.events("u", kind="tool_result")
        assert any(e["data"].get("tool") == "wallet_balances" and e["data"]["ok"] for e in results)
        assert not runtime.wallets.list("u")
    finally:
        await runtime.close()
