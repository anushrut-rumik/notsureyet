"""Status reads must leave the current question for the conversation model to answer."""

import json

import pytest

from agent.providers import Answer
from tests.test_agent import call, last_result
from tests.test_lending import USER, send
from tests.test_lending import app as app
from tests.test_lending_nlp import make_testnet


@pytest.mark.parametrize("tool", ["lending_setup_status", "lending_status"])
@pytest.mark.parametrize("status", ["ready", "deploying", "awaiting_approval", "verification_pending"])
async def test_strategy_question_continues_after_status_read(app, monkeypatch, tool, status):
    runtime, setup = make_testnet(app)
    canned = f"Setup is {status}."
    state = runtime.lending.read(USER)
    state["setup"] = {"status": status, "review": "Original setup approval review"}
    runtime.lending.save(USER, state)

    async def current_status(user):
        assert user == USER
        return {"status": status, "message": canned}

    monkeypatch.setattr(setup, "status", current_status)
    recommendation = "These test pools do not earn real interest. Let's compare their rates and fees first."

    def answer_question(_role, messages, _tools):
        assert last_result(messages)["output"]["status"] == status
        return Answer(recommendation)

    runtime.models.steps.extend([call(tool), answer_question])
    before = len(runtime.store.events(USER, kind="chat_message"))
    await send(app, "What do you suggest, how do I manage them?")
    replies = [
        e["data"]["content"]
        for e in runtime.store.events(USER, kind="chat_message")[before:]
        if e["data"]["role"] == "assistant"
    ]
    assert replies == [recommendation]
    assert not runtime.models.steps
    assert runtime.lending.read(USER)["plan"] is None


async def test_ready_status_does_not_skip_other_tools_or_plan_review(app, monkeypatch):
    runtime, setup = make_testnet(app)

    async def ready(user):
        return {"status": "ready", "message": "Setup is ready on both testnets."}

    async def portfolio(user):
        return {"availableUnits": "100", "mode": "testnet"}

    review = "Review this proposed management plan. No tokens have moved."

    async def create_plan(user, settings, **kwargs):
        assert user == USER
        return {"review": review}

    monkeypatch.setattr(setup, "status", ready)
    monkeypatch.setattr(runtime.lending, "portfolio", portfolio)
    monkeypatch.setattr(runtime.lending, "create_plan", create_plan)
    path = runtime.lending.bindings_path(USER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({USER: {"owner": runtime.wallets.agent_wallet(USER)["address"]}}))

    reads = Answer(calls=[*call("lending_setup_status").calls, *call("lending_portfolio").calls])

    def propose(_role, messages, _tools):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        assert all(r["ok"] for r in results)
        assert results[-1]["output"]["availableUnits"] == "100"
        return call("lending_create_plan")

    runtime.models.steps.extend([reads, propose])
    await send(app, "Show me the plan you suggest for managing my tokens")
    assert runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"] == review
    # Exact reviews bypass composition, but still belong in the model's conversation history.
    history = runtime.store.messages("chat:" + USER)
    assert history[-1]["role"] == "assistant"
    assert review in history[-1]["content"]
    names = [e["data"]["tool"] for e in runtime.store.events(USER, kind="tool_result")]
    assert names == ["lending_setup_status", "lending_portfolio", "lending_create_plan"]


async def test_prepare_already_ready_continues_to_answer(app, monkeypatch):
    runtime, setup = make_testnet(app)

    async def ready(user):
        return {"status": "ready", "message": "Setup is ready on both testnets."}

    monkeypatch.setattr(setup, "status", ready)
    expected = "Your setup is ready. The next step is to review a management plan."
    runtime.models.steps.extend([call("lending_prepare_setup"), Answer(expected)])
    await send(app, "What should I do next with the strategy?")
    assert runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"] == expected
    assert not runtime.models.steps
