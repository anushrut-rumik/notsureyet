"""Storage failures and slow inference must not leave WhatsApp silently stuck."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from agent.chat_errors import chat_error_message, record_chat_error
from agent.inference import InferenceError
from agent.runtime import Runtime
from agent.whatsapp import IraWhatsApp
from tests.test_agent import Script
from tests.test_web3 import config as config


def adapter(runtime, sent):
    runtime.config.env.update(
        {
            "AGENT_WHATSAPP_LINE": "line-test",
            "AGENT_WHATSAPP_ALLOWED_CHATS": "123@s.whatsapp.net",
            "AGENT_WHATSAPP_SIDECAR_URL": "http://sidecar",
            "AGENT_WHATSAPP_SECRET": "test-secret",
            "AGENT_WHATSAPP_REDIS_URL": "redis://unused",
        }
    )

    class Redis:
        async def aclose(self):
            pass

    def respond(request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"id": "reply"})

    return IraWhatsApp(
        runtime,
        redis=Redis(),
        http=httpx.AsyncClient(base_url="http://sidecar", transport=httpx.MockTransport(respond)),
    )


def incoming():
    return {
        "type": "message",
        "user": "line-test",
        "line": True,
        "id": "input-1",
        "chat": "123@s.whatsapp.net",
        "sender_lid": "456@lid",
        "text": "Check my request",
    }


def test_error_reporting_cannot_replace_a_disk_failure():
    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    error = sqlite3.OperationalError("database or disk is full")
    assert record_chat_error(SimpleNamespace(event=full), "u", error) is None
    assert "Local storage is unavailable" in chat_error_message(error)
    assert "recorded" not in chat_error_message(error)


async def test_failed_error_notice_is_recovered_once_without_replaying_the_input(config):
    runtime = Runtime(config, Script())
    sent = []
    wa = adapter(runtime, sent)
    original_notice = runtime.store.notice
    calls = []

    async def failed(*args):
        calls.append(1)
        raise sqlite3.OperationalError("database or disk is full")

    def full(*args):
        raise sqlite3.OperationalError("database or disk is full")

    runtime.chat = failed
    runtime.store.notice = full
    try:
        assert await wa.receive(incoming())
        assert wa.db.execute("SELECT state FROM inbox").fetchone()[0] == "error_pending"
        runtime.store.notice = original_notice
        await wa.deliver_once()
        await wa.deliver_once()
        assert len(sent) == 1 and "interrupted" in sent[0]
        assert calls == [1]
        assert wa.db.execute("SELECT state FROM inbox").fetchone()[0] == "interrupted"
        assert not await wa.receive(incoming())
        assert calls == [1]
    finally:
        await wa.close()
        await runtime.close()


async def test_legacy_processing_entry_gets_a_restart_notice_without_repeating_payment(config):
    runtime = Runtime(config, Script())
    sent = []
    wa = adapter(runtime, sent)
    try:
        with wa.db:
            wa.db.execute("INSERT INTO routes(user_id,chat) VALUES('u','123@s.whatsapp.net')")
            wa.db.execute("INSERT INTO inbox(key,state) VALUES('line-test:123@s.whatsapp.net:old','processing')")
        await wa.deliver_once()
        # Simulate a crash after persisting the notice but before recording recovery in the inbox.
        with wa.db:
            wa.db.execute("UPDATE inbox SET state='processing'")
        await wa.deliver_once()
        assert len(sent) == 1 and "will not repeat a payment" in sent[0]
        assert runtime.models.calls == []
        assert runtime.store.db.execute("SELECT count(*) FROM transfers").fetchone()[0] == 0
        assert wa.db.execute("SELECT state,user_id FROM inbox").fetchone() == ("interrupted", "u")
    finally:
        await wa.close()
        await runtime.close()


async def test_slow_live_chat_gets_progress_and_is_not_mistaken_for_an_interruption(config):
    runtime = Runtime(config, Script())
    sent = []
    wa = adapter(runtime, sent)
    config.env["AGENT_WHATSAPP_PROGRESS_SECONDS"] = "0.01"
    release = asyncio.Event()

    async def slow(user, text):
        await release.wait()
        runtime.store.notice(user, "The check is complete.")

    runtime.chat = slow
    request = asyncio.create_task(wa.receive(incoming()))
    try:
        await asyncio.sleep(0.03)
        await wa.deliver_once()
        assert len(sent) == 1 and "still checking" in sent[0]
        assert "interrupted" not in sent[0]
        release.set()
        await request
        await wa.deliver_once()
        assert sent[-1] == "The check is complete."
        assert len(sent) == 2
        assert wa.db.execute("SELECT state FROM inbox").fetchone()[0] == "recorded"
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await wa.close()
        await runtime.close()


async def test_chat_inference_deadline_does_not_retry_or_replay_tools(config):
    runtime = Runtime(config, Script())
    config.chat_inference_timeout = 0.01
    attempts = []

    async def slow(*args):
        attempts.append(1)
        await asyncio.Event().wait()

    runtime.models.complete = slow
    try:
        with pytest.raises(InferenceError) as captured:
            await runtime.chat("u", "Check my request")
        assert captured.value.code == "response_timeout"
        assert not captured.value.retryable
        assert attempts == [1]
        assert runtime.store.tasks("u") == []
        assert runtime.store.db.execute("SELECT count(*) FROM effects").fetchone()[0] == 0
    finally:
        await runtime.close()


async def test_redis_reconnect_after_lost_ack_does_not_execute_the_message_twice(config, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    runtime = Runtime(config, Script())
    wa = adapter(runtime, [])
    original_sleep = asyncio.sleep

    async def yield_once(_seconds):
        await original_sleep(0)

    monkeypatch.setattr("agent.whatsapp.asyncio.sleep", yield_once)

    class Redis:
        def __init__(self):
            self.groups = self.reads = self.acks = 0

        async def xgroup_create(self, *args, **kwargs):
            self.groups += 1
            if self.groups == 1:
                raise RedisConnectionError("Redis is starting")

        async def xreadgroup(self, *args, **kwargs):
            self.reads += 1
            if self.reads == 1:
                return [("stream", [])]
            if self.reads > 3:
                raise asyncio.CancelledError
            event = {**incoming(), "text": "/wallet create"}
            return [("stream", [("entry", {"data": json.dumps(event)})])]

        async def xack(self, *args):
            self.acks += 1
            if self.acks == 1:
                raise RedisConnectionError("Acknowledgement response lost")

        async def aclose(self):
            pass

    connection = Redis()
    wa.redis = connection
    try:
        with pytest.raises(asyncio.CancelledError):
            await wa.run()
        assert connection.groups == 2 and connection.acks == 2
        assert len(list(config.data_dir.glob("wallet-*.json"))) == 1
        assert runtime.store.db.execute("SELECT count(*) FROM events WHERE source='user'").fetchone()[0] == 1
        assert runtime.models.calls == []
        assert wa.db.execute("SELECT count(*) FROM inbox WHERE state='recorded'").fetchone()[0] == 1
    finally:
        await wa.close()
        await runtime.close()
