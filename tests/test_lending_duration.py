"""Sub-day durations retain their exact deadline through review, approval and expiry."""

import pytest

from agent.lending import PlanSettings
from tests.test_agent import call
from tests.test_lending import USER, send
from tests.test_lending import app as app


async def test_two_hour_plan_with_ten_minute_checks_expires_at_exact_deadline(app):
    runtime, _, clock, _ = app
    runtime.models.steps.append(call("lending_create_plan", duration_seconds=7200, check_interval_seconds=600))
    await send(app, "Do a 2 hour plan with a check every 10 minutes")
    state = runtime.lending.read(USER)
    plan = state["plan"]
    assert int(plan["policy"]["deadline"]) - clock[0] == 7200
    assert plan["settings"]["checkIntervalSeconds"] == 600
    review = runtime.store.events(USER, kind="chat_message")[-1]["data"]["content"]
    assert "2 hours" in review and "600 seconds" in review
    assert not plan["approvalEvent"]
    await send(app, "approve")
    await send(app, "/lending start")
    await runtime.lending.tick_user(USER)
    assert runtime.lending.read(USER)["plan"]["status"] == "active"
    clock[0] += 7200
    await runtime.lending.tick_user(USER)
    expired = runtime.lending.read(USER)
    assert expired["plan"]["status"] == "expired"
    assert expired["ledger"]["principalUnits"] == plan["settings"]["amountUnits"]
    assert expired["plan"]["policy"]["deadline"] == plan["policy"]["deadline"]


def test_legacy_days_and_default_are_preserved():
    base = {"amountUnits": "800", "reserveUnits": "200"}
    assert PlanSettings(**base).days == 30
    assert PlanSettings(**base, days=60).days == 60
    assert PlanSettings(**base, durationSeconds=7200).days is None


@pytest.mark.parametrize("settings", [
    {"durationSeconds": 0}, {"durationSeconds": -7200}, {"durationSeconds": 7200.5},
    {"durationSeconds": True}, {"days": 1, "durationSeconds": 7200},
])
def test_invalid_or_conflicting_durations_are_rejected(settings):
    with pytest.raises(ValueError):
        PlanSettings(amountUnits="800", reserveUnits="200", **settings)
