import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from agent.config import ROOT, Config, Endpoint
from agent.inference import InferenceError
from agent.providers import Models


@pytest.fixture
def config(tmp_path):
    (tmp_path / "inference.toml").write_text((ROOT / "inference.toml").read_text())
    return Config(tmp_path, environ={"OPENAI_API_KEY": "fake-openai", "AGENT_TASK_BACKEND": "sqlite"})


def response(*, output=None, status="completed"):
    return {
        "id": "resp-test",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-luna",
        "status": status,
        "output": output
        if output is not None
        else [
            {
                "type": "message",
                "id": "msg-1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "42", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    }


def mock_models(config, respond):
    models = Models(config)
    for name, endpoint in config.endpoints.items():
        models.clients[name] = AsyncOpenAI(
            api_key=config.key(endpoint),
            base_url=endpoint.base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
    return models


@pytest.mark.parametrize("role", ["chat", "planner", "worker"])
async def test_luna_tool_round_trip_preserves_encrypted_reasoning_and_usage(config, role):
    requests = []
    native_output = [
        {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "encrypted-state"},
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "calculate",
            "arguments": '{"expression":"6*7"}',
            "status": "completed",
        },
    ]

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "req-1"},
            json=response(output=native_output) if len(requests) == 1 else response(),
        )

    models = mock_models(config, respond)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "calculate",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    messages = [{"role": "user", "content": "Calculate 6*7"}]
    try:
        first = await models.complete(role, messages, tools)
        second = await models.complete(
            role,
            [
                *messages,
                first.message(),
                {"role": "tool", "tool_call_id": "call-1", "content": "42"},
            ],
            tools,
        )
        assert second.text == "42"
        assert first.metadata["usage"]["output_tokens_details"]["reasoning_tokens"] == 2
        assert first.metadata["request_id"] == "req-1"
        assert "metadata" not in first.message()
        assert len(requests) == 2  # No catalog or calls to a previous provider during inference.
        body = json.loads(requests[1].content)
        assert body["input"][1:3] == native_output
        assert body["input"][-1] == {"type": "function_call_output", "call_id": "call-1", "output": "42"}
        for request in requests:
            body = json.loads(request.content)
            assert str(request.url) == "https://api.openai.com/v1/responses"
            assert request.headers["authorization"] == "Bearer fake-openai"
            assert request.headers["x-client-request-id"]
            assert body["model"] == "gpt-6-luna"
            assert body["store"] is False
            assert body["include"] == ["reasoning.encrypted_content"]
            assert body["max_output_tokens"] > 0
            assert "max_tokens" not in body
            assert body["tools"][0]["name"] == "calculate"
    finally:
        await models.close()


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress", "cancelled"])
async def test_incomplete_responses_cannot_execute_tools(config, status):
    models = mock_models(config, lambda request: httpx.Response(200, json=response(status=status)))
    try:
        with pytest.raises(InferenceError, match="incomplete answer"):
            await models.complete("chat", [], [])
    finally:
        await models.close()


async def test_empty_responses_are_rejected(config):
    models = mock_models(config, lambda request: httpx.Response(200, json=response(output=[])))
    try:
        with pytest.raises(InferenceError, match="no answer or tool calls"):
            await models.complete("chat", [], [])
    finally:
        await models.close()


async def test_old_provider_state_is_not_sent_to_openai(config):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    models = mock_models(config, respond)
    try:
        await models.complete(
            "chat",
            [
                {
                    "role": "assistant",
                    "content": "Old answer",
                    "_native_id": "previous:model",
                    "reasoning_content": "previous provider reasoning",
                    "_response_items": [{"type": "reasoning"}],
                    "tool_calls": [
                        {
                            "id": "old-call",
                            "type": "function",
                            "function": {
                                "name": "calculate",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "old-call", "content": "42"},
            ],
            [],
        )
        assert bodies[0]["input"] == [
            {"role": "assistant", "content": "Old answer"},
            {"type": "function_call", "call_id": "old-call", "name": "calculate", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "old-call", "output": "42"},
        ]
    finally:
        await models.close()


@pytest.mark.parametrize("role,fields", [
    ("chat", ["emoji_reaction", "response_messages"]), ("reaction", ["label", "confidence"]),
])
async def test_chat_and_quick_reaction_request_structured_fields_in_order(config, role, fields):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    models = mock_models(config, respond)
    try:
        await models.complete(role, [{"role": "user", "content": "Hi"}], [])
        [body] = bodies
        output_format = body["text"]["format"]
        assert output_format["strict"] is True
        assert output_format["type"] == "json_schema"
        assert list(output_format["schema"]["properties"]) == fields
        assert output_format["schema"]["required"] == fields
        if role == "reaction":
            assert body["max_output_tokens"] == 128
            assert body["reasoning"] == {"effort": "none"}
            assert "tools" not in body
    finally:
        await models.close()


async def test_catalog_checks_model_access_without_paid_inference(config):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-6-luna", "object": "model", "created": 1, "owned_by": "openai"},
                ],
            },
        )

    models = mock_models(config, respond)
    try:
        assert (await models.check_catalog("chat"))["id"] == "gpt-6-luna"
        await models.check_catalog("chat")
        assert len(requests) == 1
        config.endpoints["general"] = config.endpoints["general"].model_copy(update={"model": "missing"})
        with pytest.raises(InferenceError, match="absent from the live model catalog"):
            await models.check_catalog("chat")
    finally:
        await models.close()


@pytest.mark.parametrize(
    "options",
    [
        {"stream": True},
        {"background": True},
        {"input": []},
        {"store": True},
        {"include": []},
        {"extra_body": {"tools": []}},
        {"extra_body": {"model": "other"}},
    ],
)
def test_responses_options_cannot_override_the_agent_contract(options):
    with pytest.raises(ValidationError):
        Endpoint(protocol="responses", base_url="https://api.openai.com/v1", model="gpt-6-luna", options=options)
