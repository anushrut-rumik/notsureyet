from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import ccip_simulation
from agent.arkiv import ArkivError
from agent.engine import Engine
from agent.payments import capabilities, prepare_request, register_payment_tools
from agent.tools import Registry, register_builtins
from agent.wallet import Wallets
from tests.test_agent import Script, call
from tests.test_payment_routing import payments as payments
from tests.test_web3 import config as config

RECIPIENT = "0x" + "12" * 20
ROOT = Path(__file__).resolve().parent.parent


def simulation_result(units):
    return {
        "simulator_sender_contract": "0x" + "34" * 20,
        "simulation_message_id": "0x" + "56" * 32,
        "simulated_fee_eth": "0.001",
        "recipient_balance_before": "0",
        "recipient_balance_after": ccip_simulation.decimal(units),
        "simulator_selector": "16015286601757825753",
        "duplicate_rejected": True,
    }


async def test_simulation_preserves_existing_pending_storage_and_never_signs_payments(payments, monkeypatch):
    config, store, bridge, wallets = payments
    config.env["AGENT_CCIP_MODE"] = "simulation"
    existing = store.create_task("u", "Old live task", "Do not change", "Receipt")
    pending = bridge.pending("u").copy()
    calls = []

    async def execute(config, sender, recipient, units, intent):
        calls.append((sender, recipient, units, intent))
        return simulation_result(units)

    monkeypatch.setattr(ccip_simulation, "execute", execute)
    request = dict(
        asset="CCIP-BnM",
        amount="0.001",
        source_network="ethereum_sepolia",
        destination_network="base_sepolia",
        recipient=RECIPIENT,
    )
    result = await prepare_request(wallets, "u", **request)
    assert result["status"] == "simulation_delivered"
    assert result["amount"] == "0.001"
    assert result["source_tx_hash"] is None and result["public_delivery_verified"] is False
    assert "approval_code" not in result and "transaction" not in result
    assert not wallets.list("u") and not wallets.rpc_override.calls
    assert bridge.pending("u") == pending
    assert store.task("u", existing["id"])["instruction"] == "Do not change"
    assert store.db.execute("SELECT count(*) FROM tasks WHERE user_id='u'").fetchone()[0] == 2
    assert store.task("u", result["task_id"])["status"] == "completed"
    assert result["storage_status"] == "awaiting_storage_approval"
    assert capabilities(wallets)["ccip"]["configured"]
    assert capabilities(wallets)["ccip"]["missing_setting"] is None
    # A fresh Wallets instance reads the saved result rather than executing another transfer.
    restored = Wallets(config, store, wallets.rpc_override)
    try:
        repeated = await prepare_request(restored, "u", **request)
        assert repeated["id"] == result["id"] and repeated["reused_result"]
        assert len(calls) == 1
        with pytest.raises((ValueError, ArkivError)):
            ccip_simulation.status(restored, "someone-else", result["id"])
    finally:
        await restored.close()


async def test_simulation_quote_and_live_signing_guard(payments):
    config, _, _, wallets = payments
    config.env["AGENT_CCIP_MODE"] = "simulation"
    quote = await wallets.ccip.quote("u", RECIPIENT, "0.001")
    assert quote["mode"] == "simulation" and quote["simulated_fee_eth"] == "0.001"
    assert not wallets.rpc_override.calls
    with pytest.raises(ValueError, match="local simulation mode"):
        wallets.ccip.sender()
    with pytest.raises(ValueError, match="omit task_id"):
        await prepare_request(
            wallets,
            "u",
            asset="CCIP-BnM",
            amount="0.001",
            source_network="ethereum_sepolia",
            destination_network="base_sepolia",
            recipient=RECIPIENT,
            task_id="an-existing-live-task",
        )


async def test_payment_recovery_ignores_diagnostic_json_files(payments):
    config, _, _, wallets = payments
    (config.data_dir / "payment-routing-verification.json").write_text("[]")
    wallets.restore()
    assert not wallets.list("u")


async def test_chat_returns_simulation_evidence_and_separate_storage_approval(payments, monkeypatch):
    config, store, bridge, wallets = payments
    config.env["AGENT_CCIP_MODE"] = "simulation"

    async def execute(config, sender, recipient, units, intent):
        return simulation_result(units)

    monkeypatch.setattr(ccip_simulation, "execute", execute)
    registry = Registry()
    register_builtins(registry)
    register_payment_tools(registry)
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
    engine = Engine(config, store, models, registry)
    engine.wallets = wallets
    reply = await engine.chat("u", "Simulate sending 0.001 CCIP-BnM from Sepolia to Base Sepolia")
    assert "CCIP LOCAL SIMULATION" in reply and "0.001 CCIP-BnM" in reply
    assert "Simulation message ID:" in reply and RECIPIENT in reply
    assert "real ETH and CCIP-BnM balances are unchanged" in reply
    assert "metamask" not in reply.lower() and "approve " not in reply
    assert bridge.pending("u")["public_state"]["status"] == "completed"
    assert bridge.pending("u")["public_state"]["protocol"] == "chainlink_local"
    assert not bridge.rows and not store.tasks("u")
    assert not wallets.rpc_override.calls


async def test_real_chainlink_local_runner_uses_requested_amount_and_recipient():
    root = ROOT
    if not (root / "node_modules/@foundry-rs/forge").exists() or not (root / "contracts/lib/local").exists():
        pytest.skip("Run npm ci and npm run ccip:deps for Chainlink Local integration")
    result = await ccip_simulation.execute(SimpleNamespace(root=root), "0x" + "11" * 20, RECIPIENT, 10**15, "ab" * 32)
    assert result["recipient_balance_before"] == "0"
    assert result["recipient_balance_after"] == "0.001"
    assert result["simulated_fee_eth"] == "0.001"
    assert result["duplicate_rejected"]
    assert int(result["simulation_message_id"], 16)
