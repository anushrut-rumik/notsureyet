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
    state["plan"]["status"] = "approved"  # Legacy approval-only fixture for explicit-start authorization tests.
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


@pytest.mark.parametrize("status", ["submission_unknown", "submitted", "preflight_failed"])
async def test_rebalance_progress_keeps_verified_management_active(app, tmp_path, monkeypatch, status):
    service, state, _, _ = await binding(app, tmp_path)
    state["plan"]["status"] = "active"
    error = {"code": "CRE_READ_LIMIT", "message": "CRE exceeded its contract-read limit."}

    class Child:
        returncode = 0

        async def communicate(self, payload):
            return json.dumps({"status": status, "operations": [], "execution": {"error": error}}).encode(), b""

    async def spawn(*_args, **_kwargs):
        return Child()

    monkeypatch.setattr("agent.lending_live.asyncio.create_subprocess_exec", spawn)
    await execute(service, USER, state, "evaluate")
    row = service.store.db.execute(
        "SELECT data FROM events WHERE user_id=? AND kind='lending_update' ORDER BY id DESC LIMIT 1", (USER,)
    ).fetchone()
    facts = json.loads(row[0])["facts"]
    assert facts["management_active"] is True
    assert facts["execution_error"] == error
    assert facts["broadcast_attempted"] is (status != "preflight_failed")
    assert state["error"] == "CRE_READ_LIMIT"


async def test_status_read_failure_preserves_approval_and_saved_receipts(app, tmp_path, monkeypatch):
    service, state, _, _ = await binding(app, tmp_path)
    state["plan"]["status"] = "active"
    state["liveProgress"] = {"status": "submitted", "transactions": [{"hash": "saved-receipt"}]}
    state["testnetProgress"] = {"chains": {}, "operations": []}
    service.save(USER, state)

    async def unavailable(*_args):
        raise ValueError("private provider payload must not reach chat")

    monkeypatch.setattr(service, "testnet_portfolio", unavailable)
    result = await service.status(USER)
    assert result["status"] == "active"
    assert result["plan"]["approvalEvent"] == state["plan"]["approvalEvent"]
    assert result["liveProgress"]["transactions"][0]["hash"] == "saved-receipt"
    assert result["observationFresh"] is False
    assert "saved observations" in result["refreshError"]
    assert "private provider" not in json.dumps(result)


async def test_successful_monitor_clears_a_recovered_check_error(app, tmp_path, monkeypatch):
    service, state, _, _ = await binding(app, tmp_path)
    state["plan"]["status"] = "active"
    state["error"] = "ValueError"

    class Child:
        returncode = 0

        async def communicate(self, payload):
            return b'{"status":"idle","operations":[]}', b""

    async def spawn(*_args, **_kwargs):
        return Child()

    monkeypatch.setattr("agent.lending_live.asyncio.create_subprocess_exec", spawn)
    await execute(service, USER, state, "monitor")
    assert state["error"] is None


async def test_cancelled_operation_notice_includes_verified_no_send(app, tmp_path, monkeypatch):
    service, state, _, _ = await binding(app, tmp_path)
    state["plan"]["status"] = "active"

    class Child:
        returncode = 0

        async def communicate(self, payload):
            return json.dumps({"status": "cancelled", "operations": [{
                "id": "cancelled-operation", "state": "cancelled",
                "details": {"recoveryStatus": "verified-no-send", "sourceHash": "failed-outer-receipt"},
            }]}).encode(), b""

    async def spawn(*_args, **_kwargs):
        return Child()

    monkeypatch.setattr("agent.lending_live.asyncio.create_subprocess_exec", spawn)
    await execute(service, USER, state, "monitor")
    facts = service.store.events(USER, kind="lending_update")[-1]["data"]["facts"]
    assert facts["no_token_transfer_verified"] is True
    assert facts["source_transaction_hash"] == "failed-outer-receipt"
    assert facts["destination_deposit_verified"] is False
