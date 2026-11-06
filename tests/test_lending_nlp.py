"""Natural-language channel turns and setup approval; all transaction execution is mocked."""

import json

import pytest

from agent.providers import Answer
from agent.tools import Context
from tests.test_agent import call
from tests.test_lending import USER, send
from tests.test_lending import app as app


def make_testnet(app):
    runtime = app[0]
    state = runtime.lending.read(USER)
    state["mode"] = runtime.lending.mode = "testnet"
    runtime.lending.save(USER, state)
    return runtime, runtime.lending_onboarding


def review_result(runtime):
    return {
        "reviewHash": "0x" + "ab" * 32,
        "owner": runtime.wallets.agent_wallet(USER)["address"],
        "chains": [
            {"chainId": "11155111", "pool": "0x" + "11" * 20, "wallet": "0x" + "22" * 20},
            {"chainId": "84532", "pool": "0x" + "33" * 20, "wallet": "0x" + "44" * 20},
        ],
    }


@pytest.mark.parametrize("phrase", ["go ahead", "yes please", "I approve the setup"])
async def test_natural_setup_review_approval_and_background_completion(app, monkeypatch, phrase):
    runtime, setup = make_testnet(app)
    result = review_result(runtime)
    invocations = []

    async def invoke(user, confirmation=None):
        invocations.append(confirmation)
        if not confirmation:
            return result
        path = runtime.lending.bindings_path(user)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({user: {"owner": result["owner"]}}))
        return {"status": "complete"}

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up my agent to manage the tokens with CRE")
    assert invocations == [None]
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None
    await setup.tick()
    assert invocations == [None]
    await send(app, phrase)
    assert runtime.lending.read(USER)["setup"]["status"] == "deploying"
    await send(app, phrase)  # duplicate approval cannot create another deployment
    setup.last_scan = 0
    await setup.tick()
    assert invocations == [None, result["reviewHash"]]
    assert (await setup.status(USER))["status"] == "ready"
    assert runtime.lending.read(USER)["plan"] is None
    assert "ready on both testnets" in runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]


async def test_new_payment_review_cannot_approve_old_setup(app, monkeypatch):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    runtime.store.notice(USER, "Review payment. Reply approve ABCDEF123456")
    await send(app, "go ahead")
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None


async def test_setup_tools_reject_unverified_chat_and_model_cannot_approve(app):
    runtime, setup = make_testnet(app)
    event = runtime.store.event(USER, "chat_message", {"role": "user", "content": "go ahead"}, source="user")
    ctx = Context(runtime.engine, USER, "chat:" + USER, "test", event)
    result = await runtime.registry.execute(ctx, call("lending_prepare_setup").calls[0])
    assert not result["ok"] and "authenticated WhatsApp" in result["error"]
    assert not any("approve" in name for name in runtime.registry.tools)
    with pytest.raises(ValueError, match="human approval"):
        await setup.invoke(USER, "0x" + "ab" * 32)


async def test_setup_pause_reconciles_without_starting_new_actions_and_resume_needs_approval(app, monkeypatch):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await setup.control(USER, pause=True)
    await send(app, "go ahead")
    assert runtime.lending.read(USER)["setup"]["status"] == "cancelled"
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Show me the setup again")
    await send(app, "go ahead")
    assert (await setup.control(USER, pause=True))["status"] == "paused"
    assert (await setup.control(USER, pause=False))["status"] == "deploying"


async def test_setup_receipt_uncertainty_keeps_same_review_and_reports_once(app, monkeypatch):
    runtime, setup = make_testnet(app)
    invocations = []

    async def invoke(user, confirmation=None):
        invocations.append(confirmation)
        if confirmation:
            raise TimeoutError
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await send(app, "approve")
    for _ in range(2):
        setup.last_scan = 0
        await setup.tick()
    assert invocations == [None, "0x" + "ab" * 32, "0x" + "ab" * 32]
    state = runtime.lending.read(USER)["setup"]
    assert state["status"] == "deploying" and state["error"]
    notices = [
        e
        for e in runtime.store.events(USER, kind="chat_message")
        if "waiting on a dependency" in e["data"].get("content", "")
    ]
    assert len(notices) == 1


async def test_natural_language_plan_start_status_pause_and_withdraw(app):
    runtime = app[0]
    runtime.models.steps.append(call("lending_create_plan", days=30, check_interval_seconds=60))
    await send(app, "Manage my tokens for a month and check every minute")
    state = runtime.lending.read(USER)
    assert state["plan"]["settings"]["checkIntervalSeconds"] == 60
    await send(app, "yes, go ahead")
    assert runtime.lending.read(USER)["plan"]["status"] == "approved"
    runtime.models.steps.extend([call("lending_start"), Answer("The deposit is submitted; confirmation is pending.")])
    await send(app, "Start managing them now")
    assert runtime.lending.read(USER)["operations"][0]["kind"] == "deposit"
    for _ in range(2):
        app[2][0] += 1
        await runtime.lending.tick_user(USER)
    for text, tool in [
        ("What is happening with my money?", "lending_status"),
        ("Pause management please", "lending_pause"),
        ("Return all my managed tokens to my wallet", "lending_withdraw"),
    ]:
        runtime.models.steps.extend([call(tool), Answer("Saved. I am tracking the existing operation.")])
        await send(app, text)
    assert runtime.lending.read(USER)["withdrawRequested"]
    assert not any("/lending" in e["data"].get("content", "") for e in runtime.store.events(USER, kind="chat_message"))


@pytest.mark.skipif(__import__("os").environ.get("NLP_LIVE_TEST") != "1", reason="opt-in real OpenAI inference")
async def test_real_openai_routes_natural_requests_without_executing_transactions(app):
    from agent.config import Config
    from agent.providers import Models

    runtime = app[0]
    models = Models(Config())
    ctx = Context(runtime.engine, USER, "chat:" + USER, "route-test", 0)
    cases = [
        (
            "Set up my agent so it can manage my test tokens using CRE",
            {"lending_setup_status", "lending_prepare_setup"},
        ),
        ("Manage my tokens for 30 days and check every minute", {"lending_setup_status", "lending_create_plan"}),
        ("Start managing my tokens now using the plan I already approved", {"lending_start", "lending_status"}),
        ("What is happening with my managed tokens?", {"lending_status"}),
        ("Pause management right now", {"lending_pause"}),
        ("Withdraw all my managed tokens back to my wallet", {"lending_withdraw"}),
        ("Compare the available lending pools", {"lending_markets"}),
    ]
    try:
        for text, expected in cases:
            answer = await models.complete(
                "chat",
                [
                    {"role": "system", "content": runtime.engine.system(ctx)},
                    {"role": "user", "content": text},
                ],
                runtime.registry.schemas("chat"),
            )
            names = {c["function"]["name"] for c in answer.calls}
            assert names & expected, (text, names, answer.text)
    finally:
        await models.close()


async def test_setup_broker_unlocks_only_approved_intent_and_pause_is_reconcile_only(app, monkeypatch):
    runtime, setup = make_testnet(app)
    result = review_result(runtime)
    payloads = []

    class Child:
        returncode = 0

        async def communicate(self, raw):
            data = json.loads(raw)
            payloads.append(data)
            if data["confirmation"]:
                assert data["confirmation"] == result["reviewHash"]
                assert data["key"].startswith("0x")
                return b'{"status":"paused"}', b""
            assert "key" not in data
            return json.dumps(result).encode(), b""

    async def spawn(*args, **kwargs):
        assert args[0] == "node" and args[1].endswith("scripts/part4/setup.mjs")
        assert len(args) == 2  # no signing material or user text in process arguments
        return Child()

    monkeypatch.setattr("agent.lending_onboarding.asyncio.create_subprocess_exec", spawn)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up token management")
    await send(app, "go ahead")
    await setup.control(USER, pause=True)
    setup.last_scan = 0
    await setup.tick()
    assert len(payloads) == 2 and payloads[1]["reconcileOnly"] is True
    assert runtime.lending.read(USER)["setup"]["status"] == "paused"


async def test_yes_to_unrelated_question_cannot_approve_an_older_setup(app, monkeypatch):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    runtime.store.notice(USER, "Would you like me to explain what test tokens are?")
    runtime.models.steps.append(Answer("Test tokens are for testing."))
    await send(app, "yes")
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None


@pytest.mark.skipif(__import__("os").environ.get("NLP_LIVE_TEST") != "1", reason="opt-in real OpenAI inference")
async def test_real_openai_whatsapp_setup_turn_returns_exact_review_without_deployment(app, monkeypatch):
    from agent.config import Config
    from agent.providers import Models

    runtime, setup = make_testnet(app)
    models = Models(Config())
    runtime.models = runtime.engine.models = models
    calls = []

    async def invoke(user, confirmation=None):
        calls.append(confirmation)
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    await send(app, "Set up my agent to manage my test tokens with CRE.")
    assert calls == [None]
    state = runtime.lending.read(USER)["setup"]
    assert state["status"] == "awaiting_approval" and state["approvalEvent"] is None
    assert runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"] == state["review"]
