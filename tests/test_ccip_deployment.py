import json

import pytest
from eth_abi import encode

from agent.ccip import DEST_SELECTOR, SOURCE_ROUTER, SOURCE_TOKEN, calldata
from agent.ccip_deployment import contract_address, prepare
from agent.chat_commands import ChatCommands
from agent.payments import refresh_review
from tests.test_mobile_wallet import connect
from tests.test_mobile_wallet import connected as connected
from tests.test_web3 import approve
from tests.test_web3 import config as config


@pytest.fixture
async def deployment(connected):
    config, store, bridge, chain, wallets = connected
    config.env["AGENT_CCIP_MODE"] = "testnet"
    artifact = config.root / "contracts/out/CCIPSender.sol/CCIPSender.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"bytecode": {"object": "0x60006000"}}))
    (config.root / ".env").write_text("EXISTING_SECRET=keep-this\nAGENT_CCIP_MODE=testnet\n")
    connect(wallets)
    original = chain.__call__

    async def rpc(method, params):
        if method == "eth_call":
            selector = params[0]["data"][:10]
            if selector == calldata("isChainSupported(uint64)")[:10]:
                return "0x" + encode(["bool"], [True]).hex()
            for sig, typ, value in (
                ("router()", "address", SOURCE_ROUTER),
                ("token()", "address", SOURCE_TOKEN),
                ("destination()", "uint64", DEST_SELECTOR),
            ):
                if selector == calldata(sig)[:10]:
                    return "0x" + encode([typ], [value]).hex()
        if method == "eth_getCode":
            return "0x6001"
        return await original(method, params)

    wallets.rpc_override = rpc

    async def destination_rpc(method, params):
        assert method == "eth_chainId"
        return hex(84532)

    wallets.ccip.destination_rpc_override = destination_rpc
    try:
        yield config, store, bridge, chain, wallets
    finally:
        await wallets.close()
        store.close()


async def test_deploy_reuses_metamask_authorization_and_recovers_without_second_send(deployment):
    config, store, bridge, chain, wallets = deployment
    payment = await prepare(wallets, "u")
    assert payment["kind"] == "ccip_deployment"
    assert "to" not in payment["transaction"] and payment["transaction"]["value"] == "0x0"
    assert payment["recipient"] == contract_address(payment["sender"], 0)
    assert not bridge.rows
    assert "DEPLOYMENT" in ChatCommands.review(payment)
    assert "no token transfer" in ChatCommands.review(payment)
    assert (await prepare(wallets, "u"))["id"] == payment["id"]
    with pytest.raises(ValueError, match="explicit incoming"):
        await wallets.approve("u", payment["id"])
    reviewed = await approve(wallets, store, payment)
    token = reviewed["confirmation_link"].split("/")[-1]
    request = await wallets.mobile.claim(token)
    assert request["transaction"]["data"] == payment["transaction"]["data"]
    with pytest.raises(ValueError, match="already started"):
        await wallets.mobile.claim(token)
    payment["tx_hash"] = "0x" + "ab" * 32
    chain.confirm(payment)
    chain.transaction["input"] = payment["transaction"]["data"]
    chain.transaction["to"] = None
    chain.receipt["contractAddress"] = payment["recipient"]
    assert wallets.matches(payment, chain.transaction)
    changed = {**chain.transaction, "input": "0x6000"}
    assert not wallets.matches(payment, changed)
    await wallets.mobile.attach(token, payment["tx_hash"])
    verified = await wallets.status("u", payment["id"])
    assert verified["status"] == "confirmed"
    assert config.env["AGENT_CCIP_SENDER_ADDRESS"] == payment["recipient"]
    assert "EXISTING_SECRET=keep-this" in (config.root / ".env").read_text()
    assert store.task("u", payment["task_id"])["status"] == "completed"
    assert (await prepare(wallets, "u"))["sender_contract"] == payment["recipient"]
    assert not any(method.startswith("eth_send") for method, _ in chain.calls)


async def test_unsigned_deployment_refresh_preserves_id_and_updates_predicted_address(deployment):
    _, _, _, chain, wallets = deployment
    payment = await prepare(wallets, "u")
    chain.nonce = 1
    updated = await refresh_review(wallets, "u", payment["id"])
    assert updated["id"] == payment["id"]
    assert updated["approval_code"] != payment["approval_code"]
    assert updated["recipient"] == contract_address(payment["sender"], 1)
    assert updated["recipient"] != payment["recipient"]


async def test_bad_deployment_receipt_cannot_change_configuration(deployment):
    from agent.ccip_deployment import confirmed

    config, _, _, _, wallets = deployment
    payment = await prepare(wallets, "u")
    with pytest.raises(ValueError, match="unexpected contract"):
        await confirmed(wallets, payment, {"status": "0x1", "contractAddress": "0x" + "99" * 20})
    assert not config.env.get("AGENT_CCIP_SENDER_ADDRESS")
