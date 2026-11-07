"""Approval authority is deterministic; channel wording comes from the message model."""

import hashlib
import json
import sqlite3

import pytest

from agent.channel_updates import queue_update
from agent.inference import InferenceError
from agent.providers import Answer
from tests.test_lending import USER, approved, send
from tests.test_lending import app as app
from tests.test_lending_live import binding


async def test_approval_uses_model_reply_and_preserves_exact_authority(app):
    runtime = app[0]
    await send(app, "/lending plan")
    plan = runtime.lending.read(USER)["plan"]
    expected = "Your plan is approved and I'm starting it. I'll confirm when the deposit is verified."

    def explain(_role, messages, tools):
        assert tools == []
        result = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])["application_result"]
        assert result["event"] == "plan_approved"
        assert result["start_requested"] is True
        assert result["follow_up_required"] is False
        assert result["management_active"] is False
        return Answer(expected)

    runtime.models.message_steps.append(explain)
    await send(app, "Approve")
    assert runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"] == expected
    assert runtime.lending.read(USER)["plan"]["id"] == plan["id"]
    assert runtime.lending.read(USER)["plan"]["status"] == "awaiting_deposit"
    current = runtime.lending.read(USER)["plan"]
    assert current["startRequestEvent"] == current["approvalEvent"]
    assert runtime.lending.read(USER)["operations"] == []


async def test_approval_alone_starts_and_recovers_without_a_second_instruction(app):
    from agent.runtime import Runtime
    from tests.test_agent import Script

    runtime = app[0]
    await approved(app)
    before = runtime.lending.read(USER)["plan"]
    await send(app, "approve")
    assert runtime.lending.read(USER)["plan"]["startRequestEvent"] == before["startRequestEvent"]
    restarted = Runtime(runtime.config, Script())
    restarted.lending.clock = runtime.lending.clock
    try:
        await restarted.lending.tick_user(USER)
        submitted = restarted.lending.read(USER)
        assert submitted["plan"]["id"] == before["id"]
        assert len(submitted["operations"]) == 1
        assert submitted["operations"][0]["kind"] == "deposit"
        await restarted.lending.tick_user(USER)
        assert restarted.lending.read(USER)["plan"]["status"] == "active"
        assert len(restarted.lending.read(USER)["operations"]) == 1
    finally:
        await restarted.close()


async def test_live_worker_advances_approved_start_without_an_extra_monitor_or_instruction(app, tmp_path, monkeypatch):
    service, state, _, _ = await binding(app, tmp_path)
    state["plan"]["status"] = "awaiting_deposit"
    service.save(USER, state)
    actions = []

    async def execute(_service, _user, state, action):
        actions.append(action)
        state["plan"]["status"] = "active" if len(actions) == 2 else "awaiting_deposit"
        service.save(USER, state)

    monkeypatch.setattr("agent.lending_live.execute", execute)
    await service.tick_user(USER)
    assert service.read(USER)["plan"]["status"] == "awaiting_deposit"
    await service.tick_user(USER)
    assert service.read(USER)["plan"]["status"] == "active"
    assert actions == ["start", "start"]


@pytest.mark.parametrize("settled_history", [False, True])
async def test_approved_unstarted_plan_does_not_contact_live_broker(app, tmp_path, monkeypatch, settled_history):
    service, _, _, _ = await binding(app, tmp_path)

    async def should_not_run(*_args):
        raise AssertionError("An unstarted plan must not invoke live execution")

    monkeypatch.setattr("agent.lending_live.execute", should_not_run)
    # Empty journal files left by the old premature monitor are not ongoing work.
    directory = service.runtime.config.data_dir / "lending-live" / hashlib.sha256(USER.encode()).hexdigest()
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "transactions.sqlite") as db:
        db.execute("CREATE TABLE txs(id TEXT PRIMARY KEY,state TEXT)")
        if settled_history:
            db.execute("INSERT INTO txs VALUES('old-confirmed','confirmed')")
    with sqlite3.connect(directory / "operations.sqlite") as db:
        db.execute("CREATE TABLE operations(id TEXT PRIMARY KEY,state TEXT)")
        if settled_history:
            db.execute("INSERT INTO operations VALUES('old-deposit','verified_deposit')")
    state = service.read(USER)
    state["error"] = "ValueError"
    service.save(USER, state)
    await service.tick_user(USER)
    assert service.read(USER)["error"] is None
    assert not service.store.events(USER, kind="lending_update")


async def test_existing_live_journal_is_reconciled_even_before_start(app, tmp_path, monkeypatch):
    service, _, _, _ = await binding(app, tmp_path)
    directory = service.runtime.config.data_dir / "lending-live" / hashlib.sha256(USER.encode()).hexdigest()
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "transactions.sqlite") as db:
        db.execute("CREATE TABLE txs(id TEXT PRIMARY KEY,state TEXT)")
        db.execute("INSERT INTO txs VALUES('existing','unknown')")
    actions = []

    async def monitor(_service, _user, _state, action):
        actions.append(action)

    monkeypatch.setattr("agent.lending_live.execute", monitor)
    await service.tick_user(USER)
    assert actions == ["monitor"]


async def test_failed_check_is_composed_once_without_private_error_text(app, monkeypatch):
    runtime = app[0]
    await approved(app)
    state = runtime.lending.read(USER)
    state["plan"]["status"] = "active"
    runtime.lending.save(USER, state)

    async def failed(_user):
        raise ValueError("private-provider-payload: sk-secret and /private/path")

    monkeypatch.setattr(runtime.lending, "tick_user", failed)
    for _ in range(2):
        runtime.lending.last_scan = 0
        await runtime.lending.tick()
    updates = runtime.store.events(USER, kind="lending_update")
    assert len(updates) == 1
    assert "sk-secret" not in json.dumps(updates)
    expected = "I couldn't verify the latest management check. I'm retrying before taking another action."
    runtime.models.message_steps.append(Answer(expected))
    await runtime.engine.deliver_updates()
    await runtime.engine.deliver_updates()
    replies = [e for e in runtime.store.events(USER, kind="chat_message") if e["data"].get("lendingUpdateId")]
    assert len(replies) == 1
    assert replies[0]["data"]["content"] == expected


async def test_update_survives_inference_failure_and_retry_does_not_repeat_action(app):
    runtime = app[0]
    queue_update(runtime.store, USER, "management_progress", {"status": "funding_pending"}, key="funding")
    queue_update(runtime.store, USER, "management_progress", {"status": "funding_pending"}, key="funding")

    def fail(*_args):
        raise InferenceError("connection_error", "Model unavailable")

    runtime.models.message_steps.append(fail)
    await runtime.engine.deliver_updates()
    assert not runtime.store.events(USER, kind="chat_message")
    runtime.engine.update_retry_at.clear()
    runtime.models.message_steps.append(Answer("The deposit is pending confirmation. I'll keep checking it."))
    await runtime.engine.deliver_updates()
    await runtime.engine.deliver_updates()
    assert len(runtime.store.events(USER, kind="chat_message")) == 1
    assert runtime.lending.read(USER)["operations"] == []


async def test_obsolete_error_update_is_discarded_after_recovery(app):
    runtime = app[0]
    queue_update(runtime.store, USER, "management_check_failed", {"failure_type": "ValueError"})
    await runtime.engine.deliver_updates()
    await runtime.engine.deliver_updates()
    assert not runtime.models.message_calls
    assert not runtime.store.events(USER, kind="chat_message")
    assert len(runtime.store.events(USER, kind="lending_update_discarded")) == 1


async def test_intermittent_failure_backs_off_without_repeating_warning(app, monkeypatch):
    runtime = app[0]
    await approved(app)
    now = [int(runtime.lending.clock())]
    runtime.lending.clock = lambda: now[0]
    calls = []

    async def flaky(user):
        calls.append(user)
        # A successful monitor can clear the current error before evaluation fails.
        state = runtime.lending.read(user)
        state["error"] = None
        runtime.lending.save(user, state)
        raise ValueError("private RPC URL")

    monkeypatch.setattr(runtime.lending, "tick_user", flaky)
    for elapsed in [0, 5, 30, 35, 90]:
        now[0] = int(app[0].lending.read(USER)["plan"]["policy"]["approvedAt"]) + elapsed
        runtime.lending.last_scan = 0
        await runtime.lending.tick()
    assert len(calls) == 3
    updates = runtime.store.events(USER, kind="lending_update")
    assert len(updates) == 1
    assert "private RPC" not in json.dumps(updates)
    assert runtime.lending.read(USER)["managementRetryAt"] == now[0] + 120
