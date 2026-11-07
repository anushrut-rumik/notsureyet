"""Approval must start setup before inference; status must not replay its review."""

import asyncio
import json

import pytest

from agent.approval_language import is_approval
from agent.providers import Answer
from tests.test_agent import call
from tests.test_lending import USER, send
from tests.test_lending import app as app
from tests.test_lending_nlp import make_testnet, review_result


async def reviewed(app, monkeypatch, invoke=None):
    runtime, setup = make_testnet(app)

    async def review(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke or review)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up my test tokens")
    return runtime, setup


@pytest.mark.parametrize(
    "phrase",
    [
        "Yes do this approved",
        "approved",
        "Yes, approved!",
        "Yes please approve it",
        "I approve, go ahead",
        "do this",
        "Okay, yes do this approved please",
    ],
)
async def test_approval_is_saved_without_a_model_turn(app, monkeypatch, phrase):
    runtime, _ = await reviewed(app, monkeypatch)
    before = len(runtime.models.calls)
    await send(app, phrase)
    state = runtime.lending.read(USER)["setup"]
    assert state["status"] == "deploying"
    assert len(runtime.models.calls) == before
    event = runtime.store.db.execute("SELECT data FROM events WHERE id=?", (state["approvalEvent"],)).fetchone()
    assert json.loads(event[0])["content"] == phrase
    await send(app, phrase)
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] == state["approvalEvent"]
    assert len(runtime.models.calls) == before


@pytest.mark.parametrize(
    "phrase",
    [
        "Yes do this approved if gas is free",
        "Yes do this approved but use mainnet",
        "Not approved",
        "Is this approved?",
        "Yes don't do this approved",
        "I approved the old setup",
        "approved then change the wallet",
        "approved?",
    ],
)
def test_approval_never_discards_conditions_negation_or_questions(phrase):
    assert not is_approval(phrase)


async def test_screenshot_conversation_starts_worker_and_status_reads_saved_progress(app, monkeypatch):
    runtime = app[0]
    executed = asyncio.Event()
    invocations = []
    transaction = {"action": "11155111-pool", "hash": "0x" + "12" * 32, "state": "mined"}

    async def invoke(user, confirmation=None):
        invocations.append(confirmation)
        if not confirmation:
            return review_result(runtime)
        executed.set()
        return {"status": "pending", **transaction, "transactions": [transaction]}

    runtime, setup = await reviewed(app, monkeypatch, invoke)
    await setup.notifications(USER, ready_only=True)
    before = len(runtime.models.calls)
    await send(app, "Yes do this approved")
    acknowledgement = runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]
    assert "Setup approved." in acknowledgement
    assert "only when" in acknowledgement
    assert runtime.lending.read(USER)["setup"]["status"] == "deploying"
    worker = asyncio.create_task(runtime.worker())
    try:
        await asyncio.wait_for(executed.wait(), timeout=3)
        await send(app, "What's the status")
        response = runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]
        assert "1/6" in response
        assert "Setup reference:" not in response
        assert "waiting for your approval" not in response
        assert len(runtime.models.calls) == before
        assert invocations == [None, review_result(runtime)["reviewHash"]]
        assert runtime.lending.read(USER)["plan"] is None
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_status_and_accidental_prepare_do_not_repeat_or_refresh_pending_review(app, monkeypatch):
    runtime, setup = await reviewed(app, monkeypatch)
    before = runtime.lending.read(USER)["setup"]
    calls = len(runtime.models.calls)
    await send(app, "What's the status")
    response = runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]
    assert "waiting for your approval" in response
    assert "Setup reference:" not in response
    assert len(runtime.models.calls) == calls
    # Even an incorrect model tool choice cannot regenerate an outstanding review.
    answer = "Your setup is waiting for approval of the review already shown."
    runtime.models.steps.extend([call("lending_prepare_setup"), Answer(answer)])
    await send(app, "Can you explain the current setup situation?")
    assert runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"] == answer
    assert runtime.lending.read(USER)["setup"] == before
    assert (await setup.status(USER))["status"] == "awaiting_approval"


@pytest.mark.parametrize("status", ["awaiting_approval", "paused", "cancelled"])
async def test_muting_notifications_does_not_claim_unstarted_setup_is_running(app, monkeypatch, status):
    runtime, setup = await reviewed(app, monkeypatch)
    state = runtime.lending.read(USER)
    state["setup"]["status"] = status
    runtime.lending.save(USER, state)
    response = await setup.notifications(USER, ready_only=True)
    assert "keep running" not in response
    assert runtime.lending.read(USER)["setup"]["status"] == status
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None


async def test_compound_approval_does_not_cross_a_newer_review(app, monkeypatch):
    runtime, _ = await reviewed(app, monkeypatch)
    runtime.store.notice(USER, "Review payment. Reply approve ABCDEF123456")
    await send(app, "Yes do this approved")
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None


async def test_plain_yes_after_notification_question_does_not_approve_setup(app, monkeypatch):
    runtime, _ = await reviewed(app, monkeypatch)
    runtime.store.notice(USER, "Would you like to turn off progress updates?")
    runtime.models.steps.append(Answer("Progress updates are unchanged."))
    await send(app, "yes")
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None
