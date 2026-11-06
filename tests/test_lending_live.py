"""Live broker authorization and scheduling tests; subprocess/RPC execution is mocked."""

import json

import pytest

from agent.lending import digest
from agent.lending_live import execute
from tests.test_lending import USER, approved
from tests.test_lending import app as app


async def binding(app, tmp_path):
    runtime = app[0]
    await approved(app)
    service = runtime.lending
    state = service.read(USER)
    state["mode"] = service.mode = "testnet"
    wallet = runtime.wallets.agent_wallet(USER)
    manifest = {
        "owner": wallet["address"],
        "workflowId": "test",
        "chains": [],
        "maxTransactionGasWei": "100",
        "linkFundingUnits": "100",
        "automaticExecution": True,
        "seedRun": {"fixture": True},
    }
    state["plan"]["executionBinding"] = {
        k: v for k, v in manifest.items() if k not in {"automaticExecution", "seedRun"}
    }
    state["plan"]["executionBinding"]["seedHash"] = digest(manifest["seedRun"])
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({USER: manifest}))
    runtime.config.env["AGENT_LENDING_BINDINGS"] = str(path)
    service.save(USER, state)
    return service, state, manifest, path


async def test_live_broker_rejects_changed_limits_before_accessing_signer(app, tmp_path):
    service, state, manifest, path = await binding(app, tmp_path)
    manifest["maxTransactionGasWei"] = "101"
    path.write_text(json.dumps({USER: manifest}))
    with pytest.raises(ValueError, match="new exact plan approval"):
        await execute(service, USER, state, "start")


async def test_live_broker_requires_operator_enable_and_existing_approval(app, tmp_path):
    service, state, manifest, path = await binding(app, tmp_path)
    manifest["automaticExecution"] = False
    path.write_text(json.dumps({USER: manifest}))
    with pytest.raises(ValueError, match="automaticExecution"):
        await execute(service, USER, state, "start")
    state["plan"]["approvalEvent"] = None
    with pytest.raises(ValueError, match="Approve the exact plan"):
        await execute(service, USER, state, "start")


async def test_live_broker_private_stdin_and_receipt_driven_activation(app, tmp_path, monkeypatch):
    service, state, _, _ = await binding(app, tmp_path)
    responses = ["funding_pending", "deposit_verified", "withdrawal_verified"]

    class Child:
        returncode = 0

        async def communicate(self, payload):
            data = json.loads(payload)
            assert data["key"].startswith("0x") and len(data["key"]) == 66
            assert data["user"] == USER and data["plan"]["approvalEvent"]
            return json.dumps({"status": responses.pop(0), "operations": [], "transactions": []}).encode(), b""

    async def spawn(*argv, **options):
        assert all("0x" not in arg for arg in argv)
        assert "env" not in options  # Wallet key only travels over stdin to the fixed child.
        return Child()

    monkeypatch.setattr("agent.lending_live.asyncio.create_subprocess_exec", spawn)
    await execute(service, USER, state, "start")
    assert state["plan"]["status"] == "awaiting_deposit"
    await execute(service, USER, state, "start")
    assert state["plan"]["status"] == "active"
    state["plan"]["status"] = "withdrawing"
    state["withdrawRequested"] = True
    await execute(service, USER, state, "withdraw")
    assert state["plan"]["status"] == "withdrawn"
    assert "key" not in json.dumps(service.read(USER))


async def test_live_broker_rejects_changed_valuation_seed(app, tmp_path):
    service, state, manifest, path = await binding(app, tmp_path)
    manifest["seedRun"]["fictionalPrice"] = "999"
    path.write_text(json.dumps({USER: manifest}))
    with pytest.raises(ValueError, match="new exact plan approval"):
        await execute(service, USER, state, "evaluate")


async def test_disabling_investments_does_not_disable_explicit_withdrawal(app, tmp_path, monkeypatch):
    service, state, manifest, path = await binding(app, tmp_path)
    manifest.update(automaticExecution=False, maxTransactionGasWei="999")
    manifest["seedRun"]["fictionalPrice"] = "changed"
    path.write_text(json.dumps({USER: manifest}))
    state["plan"]["status"] = "withdrawing"

    class Child:
        returncode = 0

        async def communicate(self, payload):
            data = json.loads(payload)
            assert data["manifest"]["maxTransactionGasWei"] == "100"
            return b'{"status":"withdrawal_verified","operations":[]}', b""

    async def spawn(*_args, **_kwargs):
        return Child()

    monkeypatch.setattr("agent.lending_live.asyncio.create_subprocess_exec", spawn)
    await execute(service, USER, state, "withdraw")
    assert state["plan"]["status"] == "withdrawn"


async def test_live_start_intent_survives_a_crash_before_the_broker_returns(app, tmp_path, monkeypatch):
    service, state, _, _ = await binding(app, tmp_path)
    assert state["plan"]["status"] == "approved"

    async def interrupted(*_args):
        raise RuntimeError("fixture process interrupted after first submission")

    monkeypatch.setattr("agent.lending_live.execute", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        await service.start(USER)
    assert service.read(USER)["plan"]["status"] == "awaiting_deposit"
