import json
import time

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from agent.config import Config, Endpoint
from agent.diagnostics import doctor
from agent.inference import InferenceError, retry_delay
from agent.providers import Models
from agent.runtime import Runtime
from tests.test_agent import Script


@pytest.fixture
def config(tmp_path):
    (tmp_path / "inference.toml").write_text("""[routes]
default="openai"
[endpoints.openai]
provider="openai"
base_url="https://api.openai.com/v1"
model="gpt-6-luna"
api_key_env="TEST_OPENAI_KEY"
options={max_tokens=8192, extra_body={chat_template_kwargs={enable_thinking=false}}}
""")
    return Config(tmp_path, environ={"TEST_OPENAI_KEY": "fake-openai-secret", "AGENT_TASK_BACKEND": "sqlite"})


def catalog(**overrides):
    return {
        "object": "list",
        "data": [
            {
                "id": "gpt-6-luna",
                "object": "model",
                "created": 1,
                "owned_by": "openai",
                "type": "chatbot",
                "supported_formats": ["openai"],
                "supported_parameters": ["tools"],
                "max_completion_tokens": 32768,
                **overrides,
            }
        ],
    }


def completion(*, finish="stop", message=None):
    return {
        "id": "completion-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-6-luna",
        "choices": [
            {"index": 0, "finish_reason": finish, "message": message or {"role": "assistant", "content": "done"}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def mock_models(config, respond):
    models = Models(config)
    name, endpoint = config.endpoint("chat")
    models.clients[name] = AsyncOpenAI(
        api_key=config.key(endpoint),
        base_url=endpoint.base_url,
        default_headers=endpoint.headers,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return models


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (400, "invalid_body", False),
        (401, "invalid_api_key", False),
        (402, "insufficient_balance", False),
        (403, "insufficient_scope", False),
        (429, "rate_limit_exceeded", True),
        (429, "insufficient_quota", False),
        (503, "no_available_provider", True),
    ],
)
async def test_api_errors_are_safe_and_not_retried_in_sdk(config, status, code, retryable):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        return httpx.Response(
            status,
            headers={"Retry-After": "120"},
            json={
                "error": {"code": code, "message": "fake-openai-secret should never be logged"},
                "request_id": "req-error",
            },
        )

    models = mock_models(config, respond)
    with pytest.raises(InferenceError) as caught:
        await models.complete("chat", [], [])
    exc = caught.value
    assert exc.code == code and exc.retryable == retryable
    assert exc.retry_after == 120
    assert exc.request_id == "req-error"
    assert "fake-openai-secret" not in str(exc) + json.dumps(exc.details())
    assert len(requests) == 1
    await models.close()


@pytest.mark.parametrize("finish", ["length", "content_filter", None])
async def test_incomplete_tool_output_cannot_execute(config, finish):
    def respond(request):
        data = catalog() if request.url.path.endswith("/models") else completion(finish=finish)
        return httpx.Response(200, json=data)

    models = mock_models(config, respond)
    with pytest.raises(InferenceError, match="incomplete answer"):
        await models.complete("chat", [], [])
    await models.close()


async def test_reasoning_from_other_model_is_not_replayed(config):
    bodies = []

    def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    models = mock_models(config, respond)
    await models.complete(
        "chat",
        [
            {
                "role": "assistant",
                "content": "old answer",
                "_native_id": "another:model",
                "reasoning_content": "other model reasoning",
            }
        ],
        [],
    )
    assert bodies[0]["messages"] == [{"role": "assistant", "content": "old answer"}]
    await models.close()


@pytest.mark.parametrize("retryable,delay,status", [(True, 120, "waiting"), (False, None, "needs_input")])
async def test_background_task_honors_retry_after_and_pauses_payment_errors(config, retryable, delay, status):
    class Failing(Script):
        async def complete(self, *_args):
            raise InferenceError(
                "rate_limit_exceeded" if retryable else "insufficient_balance",
                "safe error",
                retryable=retryable,
                retry_after=delay,
            )

    runtime = Runtime(config, Failing())
    try:
        task = runtime.store.create_task("u", "Task", "Do work", "Done")
        before = time.time()
        await runtime.run_once()
        saved = runtime.store.task("u", task["id"])
        assert saved["status"] == status
        assert saved["failures"] == 1
        if retryable:
            assert saved["wake_at"] >= before + delay
            assert not await runtime.run_once()
        else:
            assert saved["wake_at"] is None
    finally:
        await runtime.close()


def test_streaming_and_request_overrides_are_rejected():
    for options in (
        {"stream": True},
        {"extra_body": {"stream": True}},
        {"model": "override"},
        {"extra_body": {"tools": []}},
        {"n": 2},
    ):
        with pytest.raises(ValidationError):
            Endpoint(base_url="https://example.com", model="m", options=options)


def test_retry_after_rejects_invalid_values():
    assert retry_delay("NaN") is None
    assert retry_delay("bad-date") is None
    assert retry_delay("15") == 15


async def test_catalog_doctor_does_not_call_inference(config, monkeypatch):
    async def check(_self, _role):
        return catalog()["data"][0]

    async def forbidden(*_args):
        pytest.fail("Catalog-only diagnostics must not perform inference")

    monkeypatch.setattr(Models, "check_catalog", check)
    monkeypatch.setattr(Models, "complete", forbidden)
    report = await doctor(config)
    assert report["ok"] and not report["live_inference"]
