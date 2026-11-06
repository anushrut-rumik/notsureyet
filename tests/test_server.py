import httpx

from agent.providers import Answer
from agent.server import create_app
from tests.test_agent import Script


async def test_local_api_starts_chats_and_closes_cleanly(tmp_path):
    from agent.config import Config

    (tmp_path / "inference.toml").write_text(
        '[routes]\ndefault="test"\n[endpoints.test]\nbase_url="http://127.0.0.1:9/v1"\nmodel="test"\n'
    )
    app = create_app(
        Config(tmp_path, environ={"AGENT_TASK_BACKEND": "sqlite"}),
        models=Script(Answer("Hello from the working agent.")),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"ok": True}
            assert (await client.post("/chat", json={"message": "hello"})).json() == {
                "reply": "Hello from the working agent."
            }
            assert (await client.get("/tasks")).json() == []
            events = (await client.get("/events")).json()
            assert events[-1]["data"]["content"] == "Hello from the working agent."
