import json

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
            assert [event for event in events if event["kind"] == "chat_message"][-1]["data"]["content"] == (
                "Hello from the working agent."
            )


async def test_reaction_only_chat_publishes_an_update_to_the_original_bubble(tmp_path):
    from agent.config import Config

    (tmp_path / "inference.toml").write_text(
        '[routes]\ndefault="test"\n[endpoints.test]\nbase_url="http://unused/v1"\nmodel="test"\n'
    )
    app = create_app(Config(tmp_path, environ={"AGENT_TASK_BACKEND": "sqlite"}), models=Script(Answer(json.dumps({
        "emoji_reaction": "🙏", "response_messages": [],
    }))))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.post("/chat", json={"message": "Thanks!"})).json() == {"reply": ""}
            events = (await client.get("/events")).json()
            [original] = [event for event in events if event["kind"] == "chat_message"]
            assert original["data"]["emoji_reaction"] == "🙏" and original["data"]["answered"]
            updates = (await client.get("/events", params={"after": original["id"]})).json()
            [reaction] = [event for event in updates if event["kind"] == "chat_reaction"]
            assert reaction["data"] == {"message_event_id": original["id"], "emoji_reaction": "🙏"}
            assert (await client.get("/events", params={"user_id": "other"})).json() == []
