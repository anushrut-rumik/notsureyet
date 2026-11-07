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


@pytest.mark.parametrize(
    "phrase",
    [
        "go ahead",
        "yes please",
        "I approve the setup",
        "Cool go ahead",
        "deploy",
        "Ok deploy the transaction let's go",
    ],
)
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
        if "couldn't verify the latest setup progress" in e["data"].get("content", "")
    ]
    assert len(notices) == 1


async def test_natural_language_plan_start_status_pause_and_withdraw(app):
    runtime = app[0]
    runtime.models.steps.append(call("lending_create_plan", days=30, check_interval_seconds=60))
    await send(app, "Manage my tokens for a month and check every minute")
    state = runtime.lending.read(USER)
    assert state["plan"]["settings"]["checkIntervalSeconds"] == 60
    await send(app, "yes, go ahead")
    assert runtime.lending.read(USER)["plan"]["status"] == "awaiting_deposit"
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


@pytest.mark.parametrize("phrase", ["go ahead", "Cool go ahead", "deploy", "Ok deploy the transaction let's go"])
async def test_setup_approval_after_dead_end_status(app, monkeypatch, phrase):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    runtime.store.notice(
        USER,
        "your go-ahead is noted, but setup still shows as awaiting approval, with no deployment transaction. "
        "I don't have a control here to submit the six setup transactions. Nothing has been deployed or moved.",
    )
    await send(app, phrase)
    assert runtime.lending.read(USER)["setup"]["status"] == "deploying"


async def test_pending_setup_status_answers_question_and_review_is_explicit(app, monkeypatch):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    answer = "Setup is waiting for approval. Would you like me to show the review again?"
    runtime.models.steps.extend([call("lending_setup_status"), Answer(answer)])
    await send(app, "What happened to setup?")
    assert runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"] == answer
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Show me the review again")
    assert (
        runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]
        == runtime.lending.read(USER)["setup"]["review"]
    )
    await send(app, "Cool go ahead")
    assert runtime.lending.read(USER)["setup"]["status"] == "deploying"


@pytest.mark.parametrize("phrase", ["deploy", "don't deploy", "can you deploy?", "deploy to mainnet"])
async def test_deploy_cannot_cross_a_newer_payment_review(app, monkeypatch, phrase):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    runtime.store.notice(USER, "Review payment. Reply approve ABCDEF123456")
    runtime.models.steps.append(Answer("Please review the intended setup first."))
    await send(app, phrase)
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None


@pytest.mark.parametrize(
    "phrase", ["don't deploy", "can you deploy?", "deploy to mainnet", "go ahead but change the wallet"]
)
async def test_setup_negation_question_or_changed_scope_does_not_approve(app, monkeypatch, phrase):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    runtime.models.steps.append(Answer("Let's clarify that first."))
    await send(app, phrase)
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None


@pytest.mark.parametrize(
    "phrase",
    [
        "Ok go ahead let do this",
        "Okay, go ahead, let's do this!",
        "Cool, go ahead let's go",
        "go ahead please",
        "Yes, go ahead, lets do it",
    ],
)
async def test_whatsapp_reported_followup_reaches_executor(app, monkeypatch, phrase):
    runtime, setup = make_testnet(app)
    invocations = []

    async def invoke(user, confirmation=None):
        invocations.append(confirmation)
        if confirmation:
            return {"status": "pending", "action": "deploy-pool", "hash": "0x" + "12" * 32}
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    runtime.store.notice(
        USER, "your go-ahead is noted, but setup still shows as awaiting approval, with no deployment transaction."
    )
    await send(app, phrase)
    state = runtime.lending.read(USER)["setup"]
    assert state["status"] == "deploying"
    event = runtime.store.db.execute("SELECT data FROM events WHERE id=?", (state["approvalEvent"],)).fetchone()
    assert json.loads(event[0])["content"] == phrase  # preserve the actual human wording
    setup.last_scan = 0
    await setup.tick()
    assert invocations == [None, review_result(runtime)["reviewHash"]]
    runtime.models.steps.extend(
        [call("lending_setup_status"), Answer("Setup is deploying; its transaction is tracked.")]
    )
    await send(app, "What does this mean")
    assert runtime.lending.read(USER)["setup"]["transaction"]["hash"] == "0x" + "12" * 32


@pytest.mark.parametrize(
    "phrase",
    [
        "Ok don't go ahead let's do this",
        "go ahead but use mainnet please",
        "go ahead if gas is free",
        "go ahead?",
        "Ok deploy but change the wallet let's go",
    ],
)
async def test_conversational_padding_preserves_restrictions(app, monkeypatch, phrase):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    runtime.models.steps.append(Answer("Please clarify the requested change."))
    await send(app, phrase)
    assert runtime.lending.read(USER)["setup"]["approvalEvent"] is None


async def test_setup_ready_notice_delivered_once_after_completion(app, monkeypatch):
    runtime, setup = make_testnet(app)
    result = review_result(runtime)
    complete = False

    async def invoke(user, confirmation=None):
        if not confirmation:
            return result
        if not complete:
            return {"status": "pending", "action": "11155111-pool", "hash": "0x" + "12" * 32}
        path = runtime.lending.bindings_path(user)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({user: {"owner": result["owner"]}}))
        return {"status": "complete"}

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await send(app, "Ok go ahead let do this")
    setup.last_scan = 0
    await setup.tick()
    await app[1].deliver_once()
    assert not any("ready on both testnets" in payload.get("text", "") for _, payload in app[3])
    complete = True
    for _ in range(2):
        setup.last_scan = 0
        await setup.tick()
        await app[1].deliver_once()
    notices = [
        e
        for e in runtime.store.events(USER, kind="chat_message")
        if "ready on both testnets" in e["data"].get("content", "")
    ]
    assert len(notices) == 1
    delivered = (
        app[1].db.execute("SELECT state,provider_id FROM deliveries WHERE event_id=?", (notices[0]["id"],)).fetchone()
    )
    assert tuple(delivered) == ("accepted", "mock")
    assert sum("ready on both testnets" in payload.get("text", "") for _, payload in app[3]) == 1


async def test_parallel_setup_progress_and_finality_notice_are_not_repeated(app, monkeypatch):
    runtime, setup = make_testnet(app)
    transactions = [{"action": f"step-{i}", "hash": "0x" + f"{i:02x}" * 32, "state": "mined"} for i in range(6)]

    async def invoke(user, confirmation=None):
        if not confirmation:
            return review_result(runtime)
        return {
            "status": "pending",
            "action": "step-0",
            "hash": transactions[0]["hash"],
            "transactions": transactions,
            "mined": 6,
            "total": 6,
            "awaitingFinality": True,
        }

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await send(app, "go ahead")
    for _ in range(2):
        setup.last_scan = 0
        await setup.tick()
    assert runtime.lending.read(USER)["setup"]["status"] == "deploying"
    notices = [
        e
        for e in runtime.store.events(USER, kind="chat_message")
        if "All 6 setup transactions succeeded" in e["data"].get("content", "")
    ]
    assert len(notices) == 1
    assert "Safe confirmation unlocks this test setup" in notices[0]["data"]["content"]
    assert (await setup.status(USER))["progress"] == transactions


async def test_waiting_updates_survive_restart_and_status_answers_current_question(app, monkeypatch):
    from agent.lending_onboarding import LendingOnboarding

    runtime, setup = make_testnet(app)
    transactions = [
        {"action": chain + "-" + action, "hash": "0x" + "12" * 32, "state": "mined"}
        for chain in ["11155111", "84532"]
        for action in ["pool", "wallet", "peer"]
    ]

    async def invoke(user, confirmation=None):
        if not confirmation:
            return review_result(runtime)
        return {
            "status": "pending",
            "action": "11155111-pool",
            "hash": transactions[0]["hash"],
            "transactions": transactions,
        }

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await send(app, "go ahead")
    setup.last_scan = 0
    await setup.tick()
    first = runtime.lending.read(USER)["setup"]
    assert first["stage"] == "network_confirmation"
    assert first["lastCheckedAt"]
    restarted = LendingOnboarding(runtime.lending)
    monkeypatch.setattr(restarted, "invoke", invoke)
    before = len(runtime.store.events(USER, kind="chat_message"))
    await restarted.tick()
    assert len(runtime.store.events(USER, kind="chat_message")) == before
    state = runtime.lending.read(USER)
    state["setup"]["lastNoticeAt"] -= 121
    state["setup"]["stageStartedAt"] -= 121
    runtime.lending.save(USER, state)
    restarted.last_scan = 0
    await restarted.tick()
    last = runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]
    assert "Time in this stage: 2 minutes" in last
    assert "don't need to do anything" in last
    assert "Ethereum Sepolia, Base Sepolia" in last
    assert "reliable countdown" in last
    answer = "All six transactions succeeded. We are waiting for safe confirmation from both networks."
    runtime.models.steps.extend([call("lending_status"), Answer(answer)])
    await send(app, "What is happening with my setup?")
    last = runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]
    assert last == answer


def test_problem_status_keeps_last_verified_progress_and_check_age():
    from agent.setup_progress import setup_progress

    view = setup_progress(
        {
            "status": "deploying",
            "error": "RPC unavailable",
            "stageStartedAt": 1,
            "lastCheckedAt": 60,
            "progress": [{"action": "11155111-pool", "state": "mined"}],
        },
        241,
    )
    assert view["stage"] == "checking_problem"
    assert "Last verified: 1/6" in view["message"]
    assert "Last successful check: 3 minutes ago" in view["message"]
    assert "just now" not in view["message"]


async def test_ready_only_mutes_queued_and_future_progress_but_delivers_completion(app, monkeypatch):
    from agent.lending_onboarding import LendingOnboarding

    runtime, setup = make_testnet(app)
    complete = False
    progress = [
        {"action": chain + "-" + step, "hash": "0x" + "12" * 32, "state": "mined"}
        for chain in ["11155111", "84532"]
        for step in ["pool", "wallet", "peer"]
    ]

    async def invoke(user, confirmation=None):
        if not confirmation:
            return review_result(runtime)
        if complete:
            path = runtime.lending.bindings_path(user)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({user: {"owner": review_result(runtime)["owner"]}}))
            return {"status": "complete", "transactions": [{**t, "state": "confirmed"} for t in progress]}
        return {
            "status": "pending",
            "action": progress[0]["action"],
            "hash": progress[0]["hash"],
            "transactions": progress,
        }

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await send(app, "go ahead")
    setup.last_scan = 0
    await setup.tick()  # queued before the user's preference changes
    queued = runtime.store.events(USER, kind="chat_message")[-1]["id"]
    await send(app, "Stop updating me just update me when the whole setup is ready")
    assert runtime.lending.read(USER)["setup"]["notifyMode"] == "ready_only"
    await app[1].deliver_once()
    assert app[1].db.execute("SELECT 1 FROM deliveries WHERE event_id=?", (queued,)).fetchone() is None
    state = runtime.lending.read(USER)
    state["setup"]["lastNoticeAt"] -= 121
    runtime.lending.save(USER, state)
    restarted = LendingOnboarding(runtime.lending)
    monkeypatch.setattr(restarted, "invoke", invoke)
    before = len(runtime.store.events(USER, kind="chat_message"))
    await restarted.tick()
    assert len(runtime.store.events(USER, kind="chat_message")) == before
    assert "every 2 minutes" not in (await restarted.status(USER))["message"]
    complete = True
    for _ in range(2):
        restarted.last_scan = 0
        await restarted.tick()
        await app[1].deliver_once()
    assert sum("ready on both testnets" in p.get("text", "") for _, p in app[3]) == 1


async def test_notification_tool_saves_preference_without_pausing_setup(app, monkeypatch):
    runtime, setup = make_testnet(app)

    async def invoke(*args):
        return review_result(runtime)

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await send(app, "approve")
    for enabled in [True, False]:
        runtime.models.steps.append(call("lending_setup_notifications", ready_only=enabled))
        await send(app, "Only let me know when it is done" if enabled else "Resume setup updates")
        state = runtime.lending.read(USER)["setup"]
        assert state["status"] == "deploying"
        assert state["notifyMode"] == ("ready_only" if enabled else "progress")


async def test_safe_ready_continues_silent_finality_checks_and_blocks_on_lost_verification(app, monkeypatch):
    runtime, setup = make_testnet(app)
    finality = "pending"
    failed = False
    calls = []

    async def invoke(user, confirmation=None):
        calls.append(confirmation)
        if not confirmation:
            return review_result(runtime)
        if failed:
            raise TimeoutError
        path = runtime.lending.bindings_path(user)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({user: {"owner": review_result(runtime)["owner"]}}))
        return {"status": "complete", "finality": finality}

    monkeypatch.setattr(setup, "invoke", invoke)
    runtime.models.steps.append(call("lending_prepare_setup"))
    await send(app, "Set up management")
    await send(app, "approve")
    await setup.tick()
    assert (await setup.status(USER))["status"] == "ready"
    assert "background" in (await setup.status(USER))["message"]
    count = len(runtime.store.events(USER, kind="chat_message"))
    for failure in [True, False]:
        failed = failure
        state = runtime.lending.read(USER)
        state["setup"]["lastCheckedAt"] = 0
        runtime.lending.save(USER, state)
        setup.last_scan = 0
        await setup.tick()
        assert (await setup.status(USER))["status"] == ("verification_pending" if failure else "ready")
        assert len(runtime.store.events(USER, kind="chat_message")) == count
    finality = "finalized"
    state = runtime.lending.read(USER)
    state["setup"]["lastCheckedAt"] = 0
    runtime.lending.save(USER, state)
    setup.last_scan = 0
    await setup.tick()
    last_calls = len(calls)
    setup.last_scan = 0
    await setup.tick()
    assert len(calls) == last_calls
    assert runtime.lending.read(USER)["setup"]["finality"] == "finalized"
    assert len(runtime.store.events(USER, kind="chat_message")) == count
