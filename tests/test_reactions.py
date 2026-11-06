"""Exercise contextual inference, stored bubble updates and the WhatsApp contract."""

import asyncio
import hashlib
import json

import httpx
import pytest

from agent.inference import InferenceError
from agent.providers import Answer
from agent.reactions import EMOJI, decide, parse_reply, reaction_delay
from agent.runtime import Runtime
from agent.whatsapp import IraWhatsApp
from tests.test_agent import Script, call
from tests.test_agent import config as config


def final(emoji=None, messages=()):
    return Answer(json.dumps({"emoji_reaction": emoji, "response_messages": list(messages)}))


class Redis:
    async def aclose(self):
        pass


def incoming(message_id="original", text="Thanks!"):
    return {
        "type": "message", "user": "line-test", "line": True, "id": message_id,
        "chat": "123@s.whatsapp.net", "sender_lid": "456@lid", "text": text,
    }


def adapter(runtime, requests, *, read_status=200):
    runtime.config.env.update({
        "AGENT_WHATSAPP_LINE": "line-test", "AGENT_WHATSAPP_ALLOWED_CHATS": "123@s.whatsapp.net",
        "AGENT_WHATSAPP_SIDECAR_URL": "http://sidecar", "AGENT_WHATSAPP_SECRET": "test",
        "AGENT_WHATSAPP_REDIS_URL": "redis://unused",
    })

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(read_status if request.url.path.startswith("/read/") else 200, json={"id": "r"})

    return IraWhatsApp(runtime, redis=Redis(), http=httpx.AsyncClient(
        base_url="http://sidecar", transport=httpx.MockTransport(respond),
    ))


async def settle(runtime):
    await asyncio.gather(*runtime.engine.reaction_jobs)


@pytest.mark.parametrize("label,emoji", EMOJI.items())
async def test_quick_choice_maps_labels_and_keeps_bounded_speaker_context(label, emoji):
    models = Script()
    models.reaction_steps.append(Answer(json.dumps({"label": label, "confidence": 0.5})))
    recent = [{"role": "user" if i % 2 else "assistant", "content": str(i) * 300} for i in range(9)]
    assert await decide(models, recent, "x" * 1500) == emoji
    _, messages, tools = models.reaction_calls[0]
    assert not tools
    context = json.loads(messages[-1]["content"])
    assert context["recent_messages"] == [
        {"role": item["role"], "content": item["content"][:200]} for item in recent[-6:]
    ]
    assert context["newest_message"] == {"role": "user", "content": "x" * 1000}
    assert "calibrated probability" in messages[0]["content"]


@pytest.mark.parametrize("text", [
    '{"label":"heart","confidence":0.49}', '{"label":"heart","confidence":true}',
    '{"label":"heart","confidence":2}', '{"label":"unknown","confidence":1}', "bad JSON",
])
async def test_invalid_or_low_confidence_quick_choice_is_ignored(text):
    models = Script()
    models.reaction_steps.append(Answer(text))
    assert await decide(models, [], "Some message") is None


async def test_quick_choice_has_a_three_second_deadline_and_no_retry(monkeypatch):
    seen = []
    real_timeout = asyncio.timeout

    def timeout(seconds):
        seen.append(seconds)
        return real_timeout(0.01)

    class Slow:
        calls = 0

        async def complete(self, *_args):
            self.calls += 1
            await asyncio.Event().wait()

    monkeypatch.setattr("agent.reactions.asyncio.timeout", timeout)
    models = Slow()
    assert await decide(models, [], "Hello") is None
    assert seen == [3] and models.calls == 1


async def test_normal_reply_never_waits_for_quick_choice(config):
    started, release = asyncio.Event(), asyncio.Event()

    class Models(Script):
        async def complete(self, role, messages, tools):
            if role == "reaction":
                started.set()
                await release.wait()
                return Answer('{"label":"heart","confidence":0.9}')
            await started.wait()
            return final(None, ["You're welcome."])

    runtime = Runtime(config, Models())
    try:
        assert await asyncio.wait_for(runtime.chat("u", "Thanks!"), 0.5) == "You're welcome."
        assert not release.is_set() and runtime.engine.reaction_jobs
        release.set()
        await settle(runtime)
        [update] = runtime.store.events("u", kind="chat_reaction")
        assert update["data"]["emoji_reaction"] == "❤️"
    finally:
        await runtime.close()


async def test_early_reaction_does_not_finish_the_turn_and_main_preserves_it(config, monkeypatch):
    release, selected = asyncio.Event(), asyncio.Event()

    class Models(Script):
        async def complete(self, role, messages, tools):
            if role == "reaction":
                selected.set()
                return Answer('{"label":"eyes","confidence":0.8}')
            await release.wait()
            return final("👍", ["I checked it."])

    monkeypatch.setattr("agent.engine.reaction_delay", lambda *_: 0)
    runtime = Runtime(config, Models())
    request = asyncio.create_task(runtime.chat("u", "Can you check why this failed?"))
    try:
        await selected.wait()
        await settle(runtime)
        [message] = runtime.store.events("u", kind="chat_message")
        assert message["data"]["emoji_reaction"] == "👀"
        assert not message["data"].get("answered")
        assert not runtime.store.events("u", kind="chat_answered")
        assert not request.done()
        release.set()
        assert await request == "I checked it."
        assert len(runtime.store.events("u", kind="chat_reaction")) == 1
        assert runtime.store.events("u", kind="chat_message")[0]["data"]["emoji_reaction"] == "👀"
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await runtime.close()


async def test_reaction_only_is_read_then_reacted_persists_and_has_no_typing_or_empty_bubble(config):
    runtime = Runtime(config, Script(final("🙏")))
    requests = []
    wa = adapter(runtime, requests)
    try:
        assert await wa.receive(incoming())
        await settle(runtime)
        await wa.deliver_once()
        await wa.deliver_once()
        assert [path for path, _ in requests] == ["/read/line-test", "/react/line-test"]
        assert requests[1][1] == {
            "chat": "123@s.whatsapp.net", "id": "original", "sender": "123@s.whatsapp.net",
            "from_me": False, "emoji": "🙏",
        }
        [user] = wa.db.execute("SELECT user_id FROM routes").fetchone()
        [message] = runtime.store.events(user, kind="chat_message")
        assert message["data"]["role"] == "user"
        assert message["data"]["emoji_reaction"] == "🙏" and message["data"]["answered"]
        after = message["id"]
        assert runtime.store.events(user, after=after, kind="chat_reaction")[0]["data"] == {
            "message_event_id": after, "emoji_reaction": "🙏",
        }
        assert not await wa.receive(incoming())
        assert not runtime.store.react("other", after, "👍")
        assert not runtime.store.react(user, after, "👍")
    finally:
        await wa.close()
        await runtime.close()
    runtime = Runtime(config, Script())
    try:
        assert runtime.store.events(user, kind="chat_message")[0]["data"]["emoji_reaction"] == "🙏"
    finally:
        await runtime.close()


async def test_contextual_yes_executes_action_and_typing_ends(config):
    models = Script(call("calculate", expression="6*7"), final("👍", ["42", "That's the result."]))
    runtime = Runtime(config, models)
    user = "wa:" + hashlib.sha256(b"line-test:456@lid").hexdigest()
    runtime.store.notice(user, "Should I calculate six times seven?")
    requests = []
    wa = adapter(runtime, requests)
    try:
        assert await wa.receive(incoming(text="Yes"))
        with wa.db:
            # The question represents an earlier, already delivered bubble.
            wa.db.execute("UPDATE routes SET cursor=1 WHERE user_id=?", (user,))
        await wa.deliver_once()
        paths = [path for path, _ in requests]
        assert paths[0] == "/read/line-test"
        assert any(path == "/typing/line-test" and body["on"] for path, body in requests)
        assert [body["text"] for path, body in requests if path == "/send/line-test"] == ["42", "That's the result."]
        assert requests[-1] == ("/typing/line-test", {"to": "123@s.whatsapp.net", "on": False})
        [user] = wa.db.execute("SELECT user_id FROM routes").fetchone()
        assert runtime.store.events(user, kind="tool_result")[0]["data"]["tool"] == "calculate"
        assert "Should I calculate six times seven?" in models.calls[0][1][0]["content"]
    finally:
        await wa.close()
        await runtime.close()


@pytest.mark.parametrize("change", [
    {"type": "reaction"}, {"card_tap": True}, {"vote": {"option": "a"}}, {"poll_answer": "yes"},
    {"muted": True}, {"paused": True}, {"text": "  "}, {"text": None},
])
async def test_ineligible_events_have_no_read_typing_or_reaction(config, change):
    runtime = Runtime(config, Script())
    requests = []
    wa = adapter(runtime, requests)
    try:
        assert not await wa.receive({**incoming(), **change})
        assert requests == [] and runtime.models.calls == [] and not runtime.engine.reaction_jobs
    finally:
        await wa.close()
        await runtime.close()


async def test_read_failure_does_not_block_written_reply_or_send_reaction(config):
    runtime = Runtime(config, Script(final("👍", ["Done."])))
    requests = []
    wa = adapter(runtime, requests, read_status=500)
    try:
        assert await wa.receive(incoming())
        await wa.deliver_once()
        assert not any(path.startswith("/react/") for path, _ in requests)
        assert any(body.get("text") == "Done." for _, body in requests)
    finally:
        await wa.close()
        await runtime.close()


def test_reaction_delay_and_reply_validation(monkeypatch):
    monkeypatch.setattr("agent.reactions.random.uniform", lambda *_: 1)
    assert reaction_delay("Hello", "❤️") < reaction_delay("Hello", "👍")
    assert reaction_delay("Hello", "👍") < reaction_delay("x" * 1000, "👍")
    assert parse_reply(final("👍", ["", " yes "]).text).response_messages == ["yes"]
    with pytest.raises(InferenceError, match="invalid structured"):
        parse_reply('{"emoji_reaction":"👍"}')


async def test_main_reaction_wins_over_later_quick_choice(config, monkeypatch):
    monkeypatch.setattr("agent.engine.reaction_delay", lambda *_: 0)
    models = Script(final("🎉"))
    models.reaction_steps.append(Answer('{"label":"fire","confidence":0.9}'))
    runtime = Runtime(config, models)
    try:
        assert await runtime.chat("u", "I got the job!") == ""
        await settle(runtime)
        assert runtime.store.events("u", kind="chat_message")[0]["data"]["emoji_reaction"] == "🎉"
        assert len(runtime.store.events("u", kind="chat_reaction")) == 1
    finally:
        await runtime.close()


async def test_slow_reaction_only_decision_does_not_create_progress_bubble_or_typing(config):
    release = asyncio.Event()

    class Slow(Script):
        async def complete(self, role, messages, tools):
            if role != "reaction":
                await release.wait()
                return final("🙏")
            return Answer('{"label":"none"}')

    runtime = Runtime(config, Slow())
    requests = []
    wa = adapter(runtime, requests)
    config.env["AGENT_WHATSAPP_PROGRESS_SECONDS"] = "0.01"
    receive = asyncio.create_task(wa.receive(incoming()))
    try:
        await asyncio.sleep(0.03)
        await wa.deliver_once()
        assert [path for path, _ in requests] == ["/read/line-test"]
        release.set()
        await receive
        await wa.deliver_once()
        assert [path for path, _ in requests] == ["/read/line-test", "/react/line-test"]
    finally:
        receive.cancel()
        await asyncio.gather(receive, return_exceptions=True)
        await wa.close()
        await runtime.close()


async def test_incoming_batch_reacts_only_to_newest_eligible_message(config):
    runtime = Runtime(config, Script(final("👍"), final("🎉")))
    requests = []
    wa = adapter(runtime, requests)

    class BatchRedis(Redis):
        reads = 0
        acks = []

        async def xgroup_create(self, *_args, **_kwargs):
            pass

        async def xreadgroup(self, *_args, **_kwargs):
            self.reads += 1
            if self.reads > 1:
                raise asyncio.CancelledError
            events = [incoming("first", "Noted"), incoming("last", "I got the job!"),
                      {**incoming("poll"), "vote": {"option": "a"}}]
            return [("stream", [(str(i), {"data": json.dumps(event)}) for i, event in enumerate(events)])]

        async def xack(self, *args):
            self.acks.append(args[-1])

    wa.redis = BatchRedis()
    try:
        with pytest.raises(asyncio.CancelledError):
            await wa.run()
        await settle(runtime)
        [user] = wa.db.execute("SELECT user_id FROM routes").fetchone()
        first, last = runtime.store.events(user, kind="chat_message")
        assert not first["data"].get("emoji_reaction")
        assert last["data"]["emoji_reaction"] == "🎉"
        assert len(runtime.models.reaction_calls) == 1
        assert wa.redis.acks == ["0", "1", "2"]
    finally:
        await wa.close()
        await runtime.close()
