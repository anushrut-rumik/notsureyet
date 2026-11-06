"""Opt-in test: uses the configured OpenAI key and incurs a small inference charge."""

import json
import os

import pytest

from agent.config import Config
from agent.runtime import Runtime

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_RUN_LIVE_TESTS") != "1",
    reason="Set AGENT_RUN_LIVE_TESTS=1 to authorize paid OpenAI inference",
)


async def test_live_chat_handoff_and_verified_background_file(tmp_path):
    source = Config()
    (tmp_path / "inference.toml").write_text((source.root / "inference.toml").read_text())
    config = Config(
        tmp_path,
        environ={
            "AGENT_TASK_BACKEND": "sqlite",
            **{endpoint.api_key_env: source.key(endpoint) for endpoint in source.endpoints.values()},
        },
    )
    runtime = Runtime(config)
    try:
        reply = await runtime.engine.chat(
            "integration-test",
            "Create a background task to write openai-check.txt containing exactly "
            '"OpenAI agent integration verified". Read it back to verify the contents, '
            "then finish the task. No web research is needed.",
        )
        assert reply.strip()
        [task] = runtime.store.tasks("integration-test")
        assert task["status"] == "queued"
        assert not (config.workspace / "openai-check.txt").exists()
        assert await runtime.run_once()
        task = runtime.store.task("integration-test", task["id"])
        events = runtime.store.events("integration-test", limit=300)
        tools = [e["data"] for e in events if e["kind"] == "tool_result"]
        report = {
            "ok": task["status"] == "completed",
            "chat_reply": reply,
            "task_status": task["status"],
            "tools": [{"tool": e["tool"], "ok": e["ok"]} for e in tools],
            "inference": [e["data"] for e in events if e["kind"] == "inference_usage"],
        }
        report_path = source.data_dir / "openai-checks" / "agent-live.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2))
        assert task["status"] == "completed", task["last_error"]
        assert (config.workspace / "openai-check.txt").read_text() == "OpenAI agent integration verified"
        assert any(e["tool"] == "read_file" and e["ok"] for e in tools)
        assert any(e["tool"] == "finish_task" and e["ok"] for e in tools)
        assert len(runtime.store.events("integration-test", kind="inference_usage")) >= 3
    finally:
        await runtime.close()


async def test_live_conversation_answers_background_question_and_resumes_same_task(tmp_path):
    source = Config()
    (tmp_path / "inference.toml").write_text((source.root / "inference.toml").read_text())
    config = Config(
        tmp_path,
        environ={
            "AGENT_TASK_BACKEND": "sqlite",
            **{endpoint.api_key_env: source.key(endpoint) for endpoint in source.endpoints.values()},
        },
    )
    runtime = Runtime(config)
    user = "conversation-integration-test"
    try:
        reply = await runtime.chat(
            user,
            "Create a background task to write drink.txt. The task must first ask me which drink to use "
            "and wait for my reply. After I answer, write only the drink name into drink.txt, "
            "read it back to verify, and finish. Do not choose a drink for me.",
        )
        assert reply.strip()
        [task] = runtime.store.tasks(user)
        assert task["status"] == "queued"
        assert await runtime.run_once()
        assert runtime.store.task(user, task["id"])["status"] == "needs_input"
        assert not (config.workspace / "drink.txt").exists()
        reply = await runtime.chat(user, "Tea")
        assert reply.strip()
        [resumed] = runtime.store.tasks(user)
        assert resumed["id"] == task["id"] and resumed["status"] == "queued"
        assert await runtime.run_once()
        finished = runtime.store.task(user, task["id"])
        assert finished["status"] == "completed", finished["last_error"]
        assert (config.workspace / "drink.txt").read_text().strip() == "Tea"
        tools = runtime.store.events(user, kind="tool_result", limit=100)
        assert any(e["data"]["tool"] == "reply_to_task" and e["data"]["ok"] for e in tools)
        assert any(e["data"]["tool"] == "read_file" and e["data"]["ok"] for e in tools)
    finally:
        await runtime.close()
