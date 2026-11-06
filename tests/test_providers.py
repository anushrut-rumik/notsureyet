import json

import httpx
import pytest
from openai import AsyncOpenAI

from agent.config import Config
from agent.providers import Models


@pytest.fixture
def config(tmp_path):
    (tmp_path / "inference.toml").write_text("""[routes]
default="chat"
planner="plan"
[endpoints.chat]
base_url="https://chat.example/v1"
model="chat-model"
api_key_env="TEST_CHAT_KEY"
[endpoints.plan]
base_url="https://plan.example/v1"
model="planning-model"
api_key_env="TEST_PLANNER_KEY"
""")
    return Config(tmp_path, environ={"TEST_CHAT_KEY": "fake-chat", "TEST_PLANNER_KEY": "fake-plan"})


async def test_distinct_roles_send_distinct_credentials_and_models(config):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}
                ],
            },
        )

    models = Models(config)
    for name, endpoint in config.endpoints.items():
        models.clients[name] = AsyncOpenAI(
            api_key=config.key(endpoint),
            base_url=endpoint.base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
    for role in ("chat", "planner"):
        assert (await models.complete(role, [{"role": "user", "content": "hi"}], [])).text == "hello"
    assert [r.headers["authorization"] for r in requests] == ["Bearer fake-chat", "Bearer fake-plan"]
    assert [json.loads(r.content)["model"] for r in requests] == ["chat-model", "planning-model"]
    assert "fake-chat" not in json.dumps(config.status())
    await models.close()


async def test_missing_key_does_not_fall_back_to_another_provider(config):
    config.env["TEST_PLANNER_KEY"] = ""
    models = Models(config)
    with pytest.raises(ValueError, match="TEST_PLANNER_KEY"):
        await models.complete("planner", [], [])
    assert models.clients == {}
    await models.close()
