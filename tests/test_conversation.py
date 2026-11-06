"""Conversation regressions using isolated users, databases and simulated services."""

import json

import httpx
import pytest

from agent.arkiv import ArkivError
from agent.chat_errors import chat_error_message
from agent.config import Config
from agent.inference import InferenceError
from agent.providers import Answer
from agent.runtime import Runtime
from tests.test_agent import Script, call, last_result


@pytest.fixture
def config(tmp_path):
    (tmp_path / "inference.toml").write_text(
        '[routes]\ndefault="test"\n[endpoints.test]\nbase_url="http://unused/v1"\nmodel="test"\n'
    )
    return Config(tmp_path, environ={"AGENT_TASK_BACKEND": "sqlite"})


async def test_command_result_is_available_to_natural_followup_and_is_private(config):
    models = Script(Answer("Your wallet is on Sepolia."), Answer("Hello."))
    runtime = Runtime(config, models)
    try:
        created = await runtime.chat("alice", "/wallet create")
        address = runtime.wallets.get("alice")["address"]
        assert address in created
        await runtime.chat("alice", "What network is it on?")
        history = models.calls[0][1]
        assert any(m.get("role") == "assistant" and address in (m.get("content") or "") for m in history)
        await runtime.chat("bob", "Hi")
        assert address not in json.dumps(models.calls[1][1])
    finally:
        await runtime.close()


async def test_natural_wallet_creation_uses_tool_and_returns_result_without_task(config):
    def respond(_role, messages, _tools):
        result = last_result(messages)
        assert result["ok"]
        return Answer(f"Your wallet is {result['output']['address']} on Sepolia.")

    runtime = Runtime(config, Script(call("create_wallet"), respond))
    try:
        reply = await runtime.chat("u", "Create a wallet for me")
        assert runtime.wallets.get("u")["address"] in reply
        assert runtime.store.tasks("u") == []
        assert not runtime.wallets.list("u")
    finally:
        await runtime.close()


async def test_background_question_and_answer_resume_same_task_to_verified_completion(config):
    models = Script(call("ask_user", question="Which drink should the report cover?"))
    runtime = Runtime(config, models)
    try:
        task = runtime.store.create_task("u", "Drink report", "Write drink.txt", "The file is written and read back")
        runtime.store.notice("other", "Other user's private question")
        await runtime.run_once()

        def route_reply(_role, messages, _tools):
            prompt = messages[0]["content"]
            assert "Which drink should the report cover?" in prompt
            assert task["id"] in prompt
            assert "Other user's private question" not in prompt
            return call("reply_to_task", task_id=task["id"], message="Tea, please")

        models.steps.extend(
            [
                route_reply,
                Answer("I'll make it about tea."),
                call("write_file", filename="drink.txt", content="Tea report"),
                call("read_file", filename="drink.txt"),
                lambda _r, m, _t: call(
                    "finish_task", summary="The tea report is ready.", evidence_ids=[last_result(m)["event_id"]]
                ),
            ]
        )
        await runtime.chat("u", "Tea, please")
        assert await runtime.run_once()
        [updated] = runtime.store.tasks("u")
        assert updated["id"] == task["id"] and updated["status"] == "completed"
        assert "Tea, please" in updated["instruction"]
        assert (config.workspace / "drink.txt").read_text() == "Tea report"

        models.steps.append(Answer("Your tea report is ready."))
        await runtime.chat("u", "Is it ready?")
        assert "The tea report is ready." in models.calls[-1][1][0]["content"]
    finally:
        await runtime.close()


async def test_greeting_does_not_query_remote_tasks_but_explicit_list_fails_visibly(config):
    runtime = Runtime(config, Script(Answer("Hello."), call("list_tasks"), Answer("Task storage is unavailable.")))
    runtime.recover()

    def unavailable(_user):
        raise ArkivError("Simulated remote outage")

    runtime.store.tasks = unavailable
    try:
        assert await runtime.chat("u", "Hi") == "Hello."
        reply = await runtime.chat("u", "Show my current tasks")
        assert reply == "Task storage is unavailable."
        tools = runtime.store.events("u", kind="tool_result")
        assert tools[-1]["data"]["ok"] is False
    finally:
        await runtime.close()


async def test_failed_acknowledgement_keeps_task_and_does_not_rerun_work(config):
    def failed(*_args):
        raise InferenceError("http_503", "The inference provider could not complete the request.", retryable=True)

    models = Script(
        call("create_task", title="Report", instruction="Write a report", done_when="Verified file"), failed
    )
    runtime = Runtime(config, models)
    try:
        reply = await runtime.chat("u", "Write a report")
        assert "started" in reply
        assert len(runtime.store.tasks("u")) == 1
        assert runtime.store.tasks("u")[0]["status"] == "queued"
        assert models.calls[-1][2] == []
        assert runtime.store.events("u", kind="chat_error")[-1]["data"]["phase"] == "task_acknowledgement"
    finally:
        await runtime.close()


async def test_inference_failure_is_recorded_without_raw_body_and_next_turn_recovers(config):
    def failed(*_args):
        raise InferenceError("http_402", "Inference billing is unavailable.")

    runtime = Runtime(config, Script(failed, Answer("Hello again.")))
    try:
        with pytest.raises(InferenceError) as captured:
            await runtime.chat("u", "Hi")
        [error] = runtime.store.events("u", kind="chat_error")
        assert error["data"]["code"] == "http_402"
        assert "billing is unavailable" in chat_error_message(captured.value)
        assert "payment" not in chat_error_message(captured.value)
        assert await runtime.chat("u", "Hi again") == "Hello again."
    finally:
        await runtime.close()


async def test_whatsapp_plain_chat_failure_and_recovery_are_delivered_once(config):
    from agent.whatsapp import IraWhatsApp

    config.env.update(
        {
            "AGENT_WHATSAPP_LINE": "line-test",
            "AGENT_WHATSAPP_ALLOWED_CHATS": "123@s.whatsapp.net",
            "AGENT_WHATSAPP_SIDECAR_URL": "http://sidecar",
            "AGENT_WHATSAPP_SECRET": "test",
            "AGENT_WHATSAPP_REDIS_URL": "redis://unused",
        }
    )

    def failed(*_args):
        raise InferenceError(
            "connection_error", "Could not reach inference; check the connection.", retryable=True, retry_after=30
        )

    class Redis:
        async def aclose(self):
            pass

    sent = []

    def send(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": str(len(sent))})

    runtime = Runtime(config, Script(failed, Answer("Hi! What would you like me to do?")))
    adapter = IraWhatsApp(
        runtime, redis=Redis(), http=httpx.AsyncClient(base_url="http://sidecar", transport=httpx.MockTransport(send))
    )
    event = {
        "type": "message",
        "user": "line-test",
        "line": True,
        "chat": "123@s.whatsapp.net",
        "sender_lid": "456@lid",
        "id": "1",
        "text": "Hi",
    }
    try:
        assert await adapter.receive(event)
        await adapter.deliver_once()
        assert "Could not reach inference" in sent[0]["text"]
        assert "/tasks" not in sent[0]["text"]
        assert not await adapter.receive(event)
        assert await adapter.receive({**event, "id": "2", "text": "Hi again"})
        await adapter.deliver_once()
        await adapter.deliver_once()
        assert len(sent) == 2 and sent[1]["text"] == "Hi! What would you like me to do?"
    finally:
        await adapter.close()
        await runtime.close()


async def test_unexpected_exception_diagnostics_do_not_store_sensitive_text(config):
    def failed(*_args):
        raise RuntimeError("secret-token-do-not-log")

    runtime = Runtime(config, Script(failed))
    try:
        with pytest.raises(RuntimeError) as captured:
            await runtime.chat("u", "Hi")
        assert "secret-token-do-not-log" not in json.dumps(runtime.store.events("u"))
        assert "secret-token-do-not-log" not in chat_error_message(captured.value)
        assert runtime.store.events("u", kind="chat_error")[0]["data"]["type"] == "RuntimeError"
    finally:
        await runtime.close()


async def test_transient_model_failure_retries_inference_without_repeating_tool(config, monkeypatch):
    async def no_wait(_delay):
        pass

    monkeypatch.setattr("agent.engine.asyncio.sleep", no_wait)

    def failed(*_args):
        raise InferenceError("http_502", "The inference provider could not complete the request.", retryable=True)

    runtime = Runtime(config, Script(call("create_wallet"), failed, Answer("Your wallet is ready.")))
    try:
        assert await runtime.chat("u", "Create a wallet") == "Your wallet is ready."
        tools = runtime.store.events("u", kind="tool_result")
        assert len(tools) == 1 and tools[0]["data"]["tool"] == "create_wallet"
        assert len(runtime.store.events("u", kind="inference_retry")) == 1
        assert len(runtime.models.calls) == 3
    finally:
        await runtime.close()
