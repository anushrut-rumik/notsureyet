"""Part 4: mocked transport and ledger, real application/approval/shared financial rules.

No tests here authenticate a handset, sign a transaction, or send provider traffic.
"""

import asyncio
import hashlib
import json

import httpx
import pytest

from agent.config import Config
from agent.runtime import Runtime
from agent.tools import Context
from agent.whatsapp import IraWhatsApp
from tests.test_agent import Script, call

UNIT = 10**18
USER = "wa:" + hashlib.sha256(b"line-test:456@lid").hexdigest()


class Redis:
    async def aclose(self):
        pass


@pytest.fixture
async def app(tmp_path):
    (tmp_path / "inference.toml").write_text(
        '[routes]\ndefault="test"\n[endpoints.test]\nbase_url="http://unused/v1"\nmodel="test"\n'
    )
    config = Config(
        tmp_path,
        environ={
            "AGENT_TASK_BACKEND": "sqlite",
            "AGENT_LENDING_MODE": "mock",
            "AGENT_LENDING_EVALUATOR": "test-harness",
            "AGENT_WHATSAPP_LINE": "line-test",
            "AGENT_WHATSAPP_ALLOWED_CHATS": "123@s.whatsapp.net",
            "AGENT_WHATSAPP_SECRET": "mock-only",
            "AGENT_WHATSAPP_SIDECAR_URL": "http://sidecar",
            "AGENT_WHATSAPP_REDIS_URL": "redis://unused",
        },
    )
    runtime = Runtime(config, Script())
    runtime.wallets.create(USER)
    now = [1794045600]
    runtime.lending.clock = lambda: now[0]
    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": "mock"})

    adapter = IraWhatsApp(
        runtime,
        redis=Redis(),
        http=httpx.AsyncClient(base_url="http://sidecar", transport=httpx.MockTransport(respond)),
    )
    state = runtime.lending.read(USER)
    # Explicit fixture setup; no chat instruction is allowed to mint funds.
    state["ledger"]["idle"]["11155111"] = str(1000 * UNIT)
    state["ledger"]["fees"] = dict.fromkeys(("11155111", "84532"), "10000000")
    runtime.lending.save(USER, state)
    yield runtime, adapter, now, requests
    adapter.db.close()
    await adapter.http.aclose()
    await runtime.close()


async def send(app, text, message_id=None, **overrides):
    runtime, adapter, *_ = app
    event = {
        "type": "message",
        "user": "line-test",
        "line": True,
        "id": message_id or f"m-{len(runtime.store.events(USER))}",
        "chat": "123@s.whatsapp.net",
        "sender_lid": "456@lid",
        "text": text,
        **overrides,
    }
    return await adapter.receive(event, reactions=False)


async def approved(app, settings=None):
    runtime = app[0]
    text = "/lending plan" + (" " + json.dumps(settings) if settings else "")
    await send(app, text)
    p = runtime.lending.read(USER)["plan"]
    assert p
    await send(app, f"approve lending {p['id']} {p['version']} {p['digest'][:12]}")
    assert runtime.lending.read(USER)["plan"]["approvalEvent"]
    return p


async def deposited(app, settings=None):
    await approved(app, settings)
    await send(app, "/lending start")
    runtime = app[0]
    assert runtime.lending.read(USER)["plan"]["status"] == "awaiting_deposit"
    # Confirm initial deposit with rates equal first: scheduler must explain a hold.
    state = runtime.lending.read(USER)
    state["ledger"]["rates"]["84532"] = "400"
    runtime.lending.save(USER, state)
    await runtime.lending.tick_user(USER)
    state = runtime.lending.read(USER)
    assert state["plan"]["status"] == "active"
    assert state["operations"][0]["state"] == "verified_deposit"
    return state


async def set_move(app):
    runtime = app[0]
    state = runtime.lending.read(USER)
    state["ledger"]["rates"]["84532"] = "2000"
    state["nextCheck"] = 0
    runtime.lending.save(USER, state)


async def test_complete_mock_conversation_lifecycle(app):
    runtime, adapter, now, requests = app
    await send(
        app,
        "Manage my test tokens for 30 days. Keep some available and only move the rest "
        "when the expected extra interest covers the fees.",
    )
    p = runtime.lending.read(USER)["plan"]
    review = runtime.lending.review(p)
    assert "TEST PRESET" in review and "MOCK balances" in review
    for expected in (
        "1000",
        "800",
        "200",
        "mock-lending-sepolia",
        "mock-lending-base-sepolia",
        "3600 seconds",
        "3 SIM_USD",
        "1 SIM_USD",
        "stays deposited",
        "accrue no interest",
    ):
        assert expected in review
    await send(app, f"approve lending {p['id']} {p['version']} {p['digest'][:12]}")
    await send(app, "/lending start")
    assert runtime.lending.read(USER)["plan"]["status"] == "awaiting_deposit"
    # With 4% vs 7% over 30 days, 800 tokens do not clear costs and the minimum.
    await runtime.lending.tick_user(USER)
    state = runtime.lending.read(USER)
    assert len(state["operations"]) == 1
    assert state["lastResult"]["result"]["decision"]["action"] == "hold"
    # Changed rate makes the move worthwhile. Application scheduler now admits it.
    await set_move(app)
    now[0] += 3600
    await runtime.lending.tick()
    state = runtime.lending.read(USER)
    assert state["operations"][-1]["state"] == "submitted"
    assert state["operations"][-1]["evaluation"]["input"]["operationId"] == state["operations"][-1]["id"]
    assert state["ledger"]["idle"]["11155111"] == str(200 * UNIT)
    assert state["ledger"]["principalUnits"] == "0"
    assert runtime.lending.in_transit(state) == str(800 * UNIT)
    await runtime.lending.tick_user(USER)
    assert runtime.lending.read(USER)["operations"][-1]["state"] == "in_transit"
    await runtime.lending.tick_user(USER)
    status = await runtime.lending.status(USER)
    assert status["ledger"]["chainId"] == "84532"
    assert status["operations"][-1]["state"] == "verified_deposit"
    assert status["operations"][-1]["feeToken"]["quantityUnits"] == "2000000"
    assert status["earnedProfitUnits"] == "0"
    await send(app, "/lending status")
    await send(app, "/lending pause")
    await send(app, "/lending withdraw")
    assert runtime.lending.read(USER)["plan"]["status"] == "withdrawing"
    await runtime.lending.tick_user(USER)
    state = runtime.lending.read(USER)
    assert state["plan"]["status"] == "withdrawn"
    assert state["ledger"]["returned"] == {"11155111": str(200 * UNIT), "84532": str(800 * UNIT)}
    assert state["operations"][-1]["receipt"]["recipient"] == runtime.wallets.get(USER)["address"]
    await adapter.deliver_once()
    assert requests and all(
        data["to"] == "123@s.whatsapp.net" for path, data in requests if path.endswith("line-test") and "to" in data
    )
    assert any("Still in transit" in data.get("text", "") for _, data in requests)
    assert not runtime.models.calls  # Entire command lifecycle is deterministic; model is optional.


async def test_expert_settings_and_model_cannot_approve(app):
    runtime = app[0]
    settings = {
        "amountUnits": str(600 * UNIT),
        "reserveUnits": str(400 * UNIT),
        "days": 60,
        "checkIntervalSeconds": 600,
        "maxFeeMicrounits": "2000000",
        "minNetBenefitMicrounits": "500000",
    }
    await send(app, "/lending plan " + json.dumps(settings))
    p = runtime.lending.read(USER)["plan"]
    assert not p["preset"] and p["settings"]["days"] == 60
    assert p["policy"]["maxMovableUnits"] == str(600 * UNIT)
    event = runtime.store.events(USER, kind="chat_message")[0]["id"]
    ctx = Context(runtime.engine, USER, "chat:" + USER, "turn:test", event)
    for name, args in (("lending_approve", {"approved": True}), ("lending_start", {})):
        result = await runtime.registry.execute(ctx, {"function": {"name": name, "arguments": json.dumps(args)}})
        assert not result["ok"]
    # Exact text through unauthenticated /chat remains insufficient.
    approval = f"approve lending {p['id']} {p['version']} {p['digest'][:12]}"
    assert "authenticated WhatsApp" in await runtime.chat(USER, approval)
    assert runtime.lending.read(USER)["plan"]["approvalEvent"] is None
    with pytest.raises(ValueError):
        await runtime.lending.create_plan(USER, {**settings, "approved": True})
    with pytest.raises(ValueError):
        await runtime.lending.create_plan(USER, {**settings, "recipient": "0x" + "22" * 20})


async def test_expert_uses_identical_approval_deposit_and_policy_path(app):
    runtime = app[0]
    settings = {
        "amountUnits": str(600 * UNIT),
        "reserveUnits": str(400 * UNIT),
        "days": 60,
        "checkIntervalSeconds": 600,
    }
    state = await deposited(app, settings)
    assert state["ledger"]["principalUnits"] == str(600 * UNIT)
    assert state["ledger"]["idle"]["11155111"] == str(400 * UNIT)
    assert state["lastResult"]["result"]["decision"]["action"] == "hold"
    assert runtime.lending.read(USER)["plan"]["settings"]["checkIntervalSeconds"] == 600


async def test_changed_plan_requires_new_exact_approval_and_deadline_does_not_renew(app):
    runtime, _, now, _ = app
    old = await approved(app)
    original_deadline = old["policy"]["deadline"]
    await runtime.lending.pause(USER)
    await runtime.lending.create_plan(USER, {"amountUnits": str(700 * UNIT), "reserveUnits": str(300 * UNIT)})
    await send(app, f"approve lending {old['id']} {old['version']} {old['digest'][:12]}")
    current = runtime.lending.read(USER)["plan"]
    assert current["approvalEvent"] is None and current["version"] == "2"
    now[0] = int(original_deadline) + 1
    with pytest.raises(ValueError, match="deadline|approve"):
        await runtime.lending.start(USER)


async def test_duplicate_messages_wrong_chat_and_two_users(app):
    runtime, adapter, *_ = app
    await send(app, "/lending plan", "same-id")
    before = runtime.lending.read(USER)["plan"]["id"]
    assert not await send(app, "/lending plan", "same-id")
    assert runtime.lending.read(USER)["plan"]["id"] == before
    assert not await send(app, "/lending start", "evil", chat="unapproved@s.whatsapp.net")
    assert not await send(app, "/lending start", "history", history=True)
    await deposited(app)
    other = "wa:" + hashlib.sha256(b"line-test:other@lid").hexdigest()
    runtime.wallets.create(other)
    assert runtime.wallets.get(other)["address"] != runtime.wallets.get(USER)["address"]
    assert (await runtime.lending.status(other))["status"] == "no_plan"
    op = runtime.lending.read(USER)["operations"][0]["id"]
    with pytest.raises(ValueError, match="No such"):
        await runtime.lending.status(other, op)
    state = runtime.lending.read(USER)
    sealed = runtime.store.db.execute("SELECT sealed FROM lending_private WHERE user_id=?", (USER,)).fetchone()[0]
    assert state["plan"]["id"] not in sealed and runtime.wallets.get(USER)["address"] not in sealed
    # A route restored from disk cannot bypass a narrowed outgoing allowlist.
    adapter.allowed.clear()
    request_count = len(app[3])
    await adapter.deliver_once()
    assert len(app[3]) == request_count


async def test_pause_after_prepare_before_submission_and_nonoverlap(app):
    runtime = app[0]
    await deposited(app)
    await set_move(app)
    prepared, release = asyncio.Event(), asyncio.Event()

    async def barrier():
        prepared.set()
        await release.wait()

    job = asyncio.create_task(runtime.lending.evaluate(USER, before_submit=barrier))
    await asyncio.wait_for(prepared.wait(), 10)
    await runtime.lending.evaluate(USER)  # Prepared operation blocks another evaluation.
    await runtime.lending.pause(USER)
    release.set()
    await job
    state = runtime.lending.read(USER)
    assert state["operations"][-1]["state"] == "cancelled"
    assert state["ledger"]["principalUnits"] == str(800 * UNIT)
    assert not any(o["kind"] == "rebalance" and o["submittedAt"] for o in state["operations"])


async def test_changed_rates_between_prepare_and_submit_cancel_move(app):
    runtime = app[0]
    await deposited(app)
    await set_move(app)

    async def changed_rates():
        async with runtime.lending.lock(USER):
            state = runtime.lending.read(USER)
            state["ledger"]["rates"]["84532"] = "400"
            runtime.lending.save(USER, state)

    await runtime.lending.evaluate(USER, before_submit=changed_rates)
    assert runtime.lending.read(USER)["operations"][-1]["state"] == "cancelled"


async def test_expiry_during_transfer_tracks_and_withdraws_after_confirmation(app):
    runtime, _, now, _ = app
    await deposited(app)
    await set_move(app)
    await runtime.lending.evaluate(USER)
    state = runtime.lending.read(USER)
    deadline = state["plan"]["policy"]["deadline"]
    now[0] = int(deadline)
    await runtime.lending.tick_user(USER)
    assert (await runtime.lending.status(USER))["status"] == "expired"
    await runtime.lending.tick_user(USER)
    assert runtime.lending.read(USER)["operations"][-1]["state"] == "verified_deposit"
    assert runtime.lending.read(USER)["plan"]["policy"]["deadline"] == deadline
    await runtime.lending.withdraw(USER)
    await runtime.lending.tick_user(USER)
    assert runtime.lending.read(USER)["plan"]["status"] == "withdrawn"


async def test_restart_in_transit_and_withdraw_request_waits(app):
    runtime = app[0]
    await deposited(app)
    await set_move(app)
    await runtime.lending.evaluate(USER)
    original = runtime.lending.read(USER)["operations"][-1]["id"]
    # A second runtime opens the same durable database and encrypted vault.
    restarted = Runtime(runtime.config, Script())
    restarted.lending.clock = runtime.lending.clock
    try:
        await restarted.lending.withdraw(USER)
        await restarted.lending.tick_user(USER)
        state = restarted.lending.read(USER)
        assert state["operations"][-1]["id"] == original
        assert state["operations"][-1]["state"] == "in_transit"
        await restarted.lending.tick_user(USER)
        state = restarted.lending.read(USER)
        assert state["operations"][-2]["id"] == original
        assert state["operations"][-1]["kind"] == "withdraw"
        await restarted.lending.tick_user(USER)
        assert restarted.lending.read(USER)["plan"]["status"] == "withdrawn"
    finally:
        await restarted.close()


async def test_missing_funds_dependency_and_testnet_do_not_fake_execution(app):
    runtime = app[0]
    state = runtime.lending.read(USER)
    state["ledger"]["idle"]["11155111"] = "0"
    runtime.lending.save(USER, state)
    with pytest.raises(ValueError, match="No test CCIP-BnM"):
        await runtime.lending.create_plan(USER)
    state["ledger"]["idle"]["11155111"] = str(1000 * UNIT)
    runtime.lending.save(USER, state)
    await deposited(app)
    await set_move(app)

    async def unavailable(_request):
        raise ValueError("CRE is unavailable")

    runtime.lending.rules = unavailable
    await runtime.lending.tick()
    state = runtime.lending.read(USER)
    assert len(state["operations"]) == 1
    assert state["error"] == "ValueError"
    runtime.lending.mode = "testnet"
    with pytest.raises(ValueError, match="AGENT_LENDING_BINDINGS"):
        await runtime.lending.testnet_portfolio(USER, runtime.wallets.get(USER)["address"])


async def test_tools_require_authenticated_context_and_forbid_identity_overrides(app):
    runtime = app[0]
    event = runtime.store.event(USER, "chat_message", {"role": "user", "content": "hello"}, source="user")
    ctx = Context(runtime.engine, USER, "chat:" + USER, "turn:test", event)
    result = await runtime.registry.execute(ctx, {"function": {"name": "lending_status", "arguments": "{}"}})
    assert not result["ok"] and "authenticated WhatsApp" in result["error"]
    schemas = [t for t in runtime.registry.schemas("chat") if t["function"]["name"].startswith("lending_")]
    assert len(schemas) == 9
    for tool in schemas:
        properties = tool["function"]["parameters"]["properties"]
        assert not {"user_id", "wallet", "recipient", "approved", "calldata", "command"}.intersection(properties)


async def test_model_selected_plan_emits_exact_review_without_model_rewriting(app):
    runtime = app[0]
    runtime.models.steps.append(call("lending_create_plan"))
    await send(app, "Please suggest a plan for my test tokens")
    state = runtime.lending.read(USER)
    review = runtime.lending.review(state["plan"])
    replies = runtime.store.events(USER, kind="chat_message")
    assert replies[-1]["data"]["content"] == review
    assert state["plan"]["approvalEvent"] is None
    assert len(runtime.models.calls) == 1


async def test_missing_funds_after_approval_never_enables_management(app):
    runtime = app[0]
    await approved(app)
    state = runtime.lending.read(USER)
    state["ledger"]["idle"]["11155111"] = "0"
    runtime.lending.save(USER, state)
    with pytest.raises(ValueError, match="Funding is missing"):
        await runtime.lending.start(USER)
    assert runtime.lending.read(USER)["operations"] == []
    assert runtime.lending.read(USER)["plan"]["status"] == "approved"


async def test_stale_prepared_recovery_and_unknown_outcome_stays_locked(app):
    runtime, _, now, _ = app
    await deposited(app)
    state = runtime.lending.read(USER)
    op = runtime.lending.operation(state, "rebalance", state["ledger"]["principalUnits"], "11155111")
    runtime.lending.save(USER, state)
    now[0] += 301
    restarted = Runtime(runtime.config, Script())
    restarted.lending.clock = runtime.lending.clock
    try:
        await restarted.lending.tick_user(USER)
        state = restarted.lending.read(USER)
        assert next(o for o in state["operations"] if o["id"] == op["id"])["state"] == "cancelled"
        unknown = restarted.lending.operation(state, "rebalance", state["ledger"]["principalUnits"], "11155111")
        unknown["state"] = "unknown"
        restarted.lending.save(USER, state)
        await restarted.lending.withdraw(USER)
        await restarted.lending.tick_user(USER)
        state = restarted.lending.read(USER)
        assert state["plan"]["status"] == "withdrawing"
        assert state["operations"][-1]["state"] == "unknown"
        assert not any(o["kind"] == "withdraw" for o in state["operations"])
    finally:
        await restarted.close()


async def test_native_cre_evaluation_opt_in(app):
    # Separate real native execution acceptance, still using labelled arithmetic fixtures.
    import os

    if os.environ.get("LENDING_NATIVE_TEST") != "1":
        pytest.skip("Set LENDING_NATIVE_TEST=1 to invoke the actual CRE CLI (requires its existing login)")
    import time

    runtime = app[0]
    runtime.lending.clock = time.time
    await deposited(app)
    runtime.lending.native = True
    await set_move(app)
    result = await runtime.lending.evaluate(USER)
    assert result["engine"] == "native-local-CRE"
    assert result["operation"]["state"] == "submitted"


async def test_plain_approve_binds_latest_shown_lending_plan_and_authenticated_user(app):
    runtime = app[0]
    await send(app, "/lending plan")
    assert "authenticated WhatsApp" in await runtime.chat(USER, "approve")
    assert runtime.lending.read(USER)["plan"]["approvalEvent"] is None
    await send(app, "Approve it")
    first = runtime.lending.read(USER)["plan"]["approvalEvent"]
    assert first
    await send(app, "approve")
    assert runtime.lending.read(USER)["plan"]["approvalEvent"] == first
    assert not runtime.models.calls


async def test_plain_approval_cannot_authorize_unseen_changed_lending_plan(app):
    runtime = app[0]
    await send(app, "/lending plan")
    await runtime.lending.create_plan(USER, {"amountUnits": str(700 * UNIT), "reserveUnits": str(300 * UNIT)})
    await send(app, "approve")
    assert runtime.lending.read(USER)["plan"]["approvalEvent"] is None


async def test_newer_payment_review_prevents_plain_lending_approval(app):
    runtime = app[0]
    await send(app, "/lending plan")
    runtime.store.notice(USER, "Review payment. Reply approve AAAAAAAAAAAA")
    await send(app, "approve")
    assert runtime.lending.read(USER)["plan"]["approvalEvent"] is None
