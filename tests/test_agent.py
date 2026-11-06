import asyncio
import json
import time
from collections import deque
from datetime import UTC, datetime, timedelta

import pytest

from agent.config import Config
from agent.providers import Answer
from agent.runtime import Runtime
from agent.tools import Context


def call(name, **arguments):
    return Answer(
        calls=[
            {
                "id": f"call-{name}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ]
    )


class Script:
    def __init__(self, *steps):
        self.steps, self.calls = deque(steps), []
        self.reaction_steps, self.reaction_calls = deque(), []

    async def complete(self, role, messages, tools):
        if role == "reaction":
            self.reaction_calls.append((role, messages, tools))
            return self.reaction_steps.popleft() if self.reaction_steps else Answer('{"label":"none"}')
        self.calls.append((role, messages, tools))
        step = self.steps.popleft()
        return step(role, messages, tools) if callable(step) else step

    async def close(self):
        pass


@pytest.fixture
def config(tmp_path):
    (tmp_path / "inference.toml").write_text("""[routes]
default="test"
[endpoints.test]
base_url="http://127.0.0.1:9/v1"
model="test"
""")
    return Config(tmp_path, environ={"AGENT_TASK_BACKEND": "sqlite"})


def last_result(messages):
    return json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))


async def test_chat_hands_off_task_and_worker_creates_verified_file(config):
    def finish(role, messages, _tools):
        result = last_result(messages)
        assert result["ok"]
        return call("finish_task", summary="The report is ready.", evidence_ids=[result["event_id"]])

    models = Script(
        call(
            "create_task", title="Write report", instruction="Write a report about tea", done_when="report.txt exists"
        ),
        Answer("I'll write the tea report and check the file before sending the result."),
        call("write_file", filename="report.txt", content="Tea report"),
        call("read_file", filename="report.txt"),
        finish,
    )
    runtime = Runtime(config, models)
    assert await runtime.engine.chat("u", "Write a report about tea") == (
        "I'll write the tea report and check the file before sending the result."
    )
    [task] = runtime.store.tasks("u")
    assert task["status"] == "queued"
    assert await runtime.run_once()
    assert runtime.store.task("u", task["id"])["status"] == "completed"
    assert (config.workspace / "report.txt").read_text() == "Tea report"
    assert [entry[0] for entry in models.calls] == ["chat", "chat", "planner", "planner", "planner"]
    await runtime.close()


async def test_memory_survives_restart_and_is_in_next_prompt(config):
    runtime = Runtime(config, Script(call("remember", key="drink", content="Tea without sugar"), Answer("Remembered.")))
    await runtime.engine.chat("u", "I prefer tea without sugar")
    await runtime.close()
    models = Script(Answer("Tea without sugar."))
    runtime = Runtime(config, models)
    await runtime.engine.chat("u", "What do I like?")
    assert "Tea without sugar" in models.calls[0][1][0]["content"]
    assert runtime.store.memories("other") == []
    await runtime.close()


async def test_timer_survives_restart_and_reminder_completes_with_receipt(config):
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    runtime = Runtime(config, Script(call("wait_until", until=until, reason="Wait for the reminder time")))
    task = runtime.store.create_task("u", "Reminder", "Remind me", "Reminder delivered at its time")
    await runtime.run_once()
    assert runtime.store.task("u", task["id"])["status"] == "waiting"
    await runtime.close()
    runtime = Runtime(
        config,
        Script(
            call("notify_user", message="Your reminder is due."),
            lambda _r, m, _t: call(
                "finish_task", summary="Reminder delivered.", evidence_ids=[last_result(m)["event_id"]]
            ),
        ),
    )
    runtime.store.recover()
    assert not await runtime.run_once()
    runtime.store.update_task("u", task["id"], wake_at=time.time() - 1)
    assert await runtime.run_once()
    assert runtime.store.task("u", task["id"])["status"] == "completed"
    assert any(event["data"].get("content") == "Your reminder is due." for event in runtime.store.events("u"))
    await runtime.close()


async def test_completion_cannot_cite_invented_or_other_tasks_evidence(config):
    runtime = Runtime(config, Script())
    task = runtime.store.create_task("u", "Job", "Do work", "File exists")
    ctx = Context(runtime.engine, "u", "task", task["id"], 1, "task", task["id"])
    result = await runtime.registry.execute(ctx, call("finish_task", summary="Done", evidence_ids=[999]).calls[0])
    assert not result["ok"]
    assert runtime.store.task("u", task["id"])["status"] == "queued"
    foreign = runtime.store.event("other", "tool_result", {"ok": True, "evidence": True}, task_id=task["id"])
    assert not runtime.store.evidence("u", task["id"], [foreign])
    await runtime.close()


async def test_successful_effect_is_not_repeated_and_unknown_effect_is_not_retried(config):
    runtime = Runtime(config, Script())
    task = runtime.store.create_task("u", "Write", "Write a file", "File verified")
    ctx = Context(runtime.engine, "u", "task", task["id"], 1, "task", task["id"])
    action = call("write_file", filename="result.txt", content="original").calls[0]
    one = await runtime.registry.execute(ctx, action)
    (config.workspace / "result.txt").write_text("changed elsewhere")
    two = await runtime.registry.execute(ctx, action)
    assert one == two
    assert (config.workspace / "result.txt").read_text() == "changed elsewhere"
    count = []

    @runtime.registry.tool(effect=True)
    async def interrupted(ctx):
        """Simulate an interrupted external action."""
        count.append(1)
        raise asyncio.CancelledError

    action = call("interrupted").calls[0]
    with pytest.raises(asyncio.CancelledError):
        await runtime.registry.execute(ctx, action)
    runtime.store.recover()
    assert not (await runtime.registry.execute(ctx, action))["ok"]
    assert count == [1]
    await runtime.close()


async def test_step_budget_requeues_work_instead_of_losing_it(config):
    config.steps_per_run = 1
    runtime = Runtime(config, Script(call("save_progress", notes="First step done; next verify")))
    task = runtime.store.create_task("u", "Job", "Do work", "Verified outcome")
    await runtime.run_once()
    task = runtime.store.task("u", task["id"])
    assert task["status"] == "queued"
    assert "next verify" in task["notes"]
    await runtime.close()


async def test_missing_input_pauses_and_user_reply_resumes(config):
    runtime = Runtime(config, Script(call("ask_user", question="Which date?")))
    task = runtime.store.create_task("u", "Job", "Do work", "Verified outcome")
    await runtime.run_once()
    assert runtime.store.task("u", task["id"])["status"] == "needs_input"
    ctx = Context(runtime.engine, "u", "chat", "turn", 1)
    result = await runtime.registry.execute(ctx, call("reply_to_task", task_id=task["id"], message="Tomorrow").calls[0])
    assert result["ok"]
    assert runtime.store.task("u", task["id"])["status"] == "queued"
    await runtime.close()


async def test_cancelled_task_does_not_run_next_tool_in_same_step(config):
    runtime = Runtime(config, Script())
    task = runtime.store.create_task("u", "Job", "Do work", "Verified outcome")

    @runtime.registry.tool()
    async def cancel_during_call(ctx):
        """Represent a cancellation arriving while an action is in flight."""
        ctx.store.update_task("u", task["id"], status="cancelled")
        return "cancelled"

    runtime.models.steps.append(
        Answer(
            calls=[
                call("cancel_during_call").calls[0],
                call("write_file", filename="never.txt", content="must not write").calls[0],
            ]
        )
    )
    await runtime.run_once()
    assert not (config.workspace / "never.txt").exists()
    await runtime.close()


async def test_custom_tool_can_be_registered_and_used(config):
    config.tool_modules = ["examples.my_tools"]
    runtime = Runtime(config, Script())
    (config.workspace / "words.txt").write_text("one two three")
    ctx = Context(runtime.engine, "u", "chat", "turn", 1)
    result = await runtime.registry.execute(ctx, call("word_count", filename="words.txt").calls[0])
    assert result["output"]["words"] == 3
    assert not (await runtime.registry.execute(ctx, call("read_file", filename="../.env").calls[0]))["ok"]
    await runtime.close()


async def test_task_handoff_stops_chat_from_running_same_turn_work(config):
    models = Script(
        Answer(
            calls=[
                call(
                    "create_task", title="Write report", instruction="Write a report", done_when="File verified"
                ).calls[0],
                call("write_file", filename="premature.txt", content="wrong session").calls[0],
            ]
        ),
        Answer("I'll write the report in the background and verify it."),
    )
    runtime = Runtime(config, models)
    try:
        reply = await runtime.engine.chat("u", "Create a background task to write a report")
        assert "background" in reply
        assert len(models.calls) == 2
        assert models.calls[-1][2] == []  # acknowledgement has no action tools
        assert len(runtime.store.tasks("u")) == 1
        assert not (config.workspace / "premature.txt").exists()
        result = last_result(runtime.store.messages("chat:u"))
        assert not result["ok"] and "background task" in result["error"]
    finally:
        await runtime.close()
