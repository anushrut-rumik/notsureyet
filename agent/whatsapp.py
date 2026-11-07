"""Use Ira's existing whatsmeow sidecar contract: Redis events in, POST /send/{line} out.

This is a separate consumer for a dedicated hackathon line. It never imports or
changes the reference production service, pairs an account, or starts a sidecar.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
from urllib.parse import quote

import httpx

from agent.chat_errors import chat_error_message


class WhatsAppTurn:
    """The channel actions for one original user message."""

    def __init__(self, adapter, event, reactions):
        self.adapter, self.event, self.reactions = adapter, event, reactions
        self.typing = False
        self.metadata = {
            "name": "whatsapp", "chat": event["chat"], "message_id": event["id"],
            "verified_transport": True,
            # Direct-message receipts/reactions key the address from the chat.
            # The stable LID identifies the owner; it may be a different address.
            "sender": event["chat"],
        }

    async def read(self):
        return await self.adapter.signal("read", {
            "chat": self.event["chat"], "ids": [self.event["id"]], "sender": self.metadata["sender"],
        })

    async def working(self):
        self.adapter.working_chats.add(self.event["chat"])
        if not self.typing:
            self.typing = await self.adapter.signal("typing", {"to": self.event["chat"], "on": True})

    async def finish(self):
        requested = self.event["chat"] in self.adapter.working_chats
        self.adapter.working_chats.discard(self.event["chat"])
        if requested:
            # An on-request can time out after succeeding at the sidecar.
            await self.adapter.signal("typing", {"to": self.event["chat"], "on": False})
            self.typing = False


class IraWhatsApp:
    group = "ira-web3-agent"

    def __init__(self, runtime, *, redis=None, http=None):
        self.runtime = runtime
        self.active_inputs = set()
        self.working_chats = set()
        env = runtime.config.env
        required = [
            "AGENT_WHATSAPP_LINE",
            "AGENT_WHATSAPP_ALLOWED_CHATS",
            "AGENT_WHATSAPP_SIDECAR_URL",
            "AGENT_WHATSAPP_SECRET",
            "AGENT_WHATSAPP_REDIS_URL",
        ]
        if any(not env.get(key) for key in required):
            raise ValueError("Configure the AGENT_WHATSAPP_* settings for an existing, dedicated Ira sidecar line")
        self.line = env["AGENT_WHATSAPP_LINE"]
        if not re.fullmatch(r"line-[a-zA-Z0-9_-]+", self.line):
            raise ValueError("AGENT_WHATSAPP_LINE must name the sidecar's line-… session")
        self.allowed = set(env["AGENT_WHATSAPP_ALLOWED_CHATS"].split(","))
        self.stream = env.get("AGENT_WHATSAPP_STREAM") or "ira:wa:in"
        if redis is None:
            import redis.asyncio as aioredis

            redis = aioredis.from_url(
                env["AGENT_WHATSAPP_REDIS_URL"], decode_responses=True, socket_connect_timeout=5, socket_timeout=10
            )
        self.redis = redis
        self.http = http or httpx.AsyncClient(
            base_url=env["AGENT_WHATSAPP_SIDECAR_URL"],
            headers={"Authorization": "Bearer " + env["AGENT_WHATSAPP_SECRET"]},
            timeout=30,
        )
        path = runtime.config.data_dir / "whatsapp.sqlite3"
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS routes (user_id TEXT PRIMARY KEY, chat TEXT NOT NULL, cursor INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS deliveries (event_id INTEGER PRIMARY KEY, state TEXT NOT NULL, provider_id TEXT);
        """)
        if "user_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(inbox)")}:
            with self.db:
                self.db.execute("ALTER TABLE inbox ADD COLUMN user_id TEXT")

        floor = self.db.execute("SELECT coalesce(max(event_id),0) FROM deliveries").fetchone()[0]
        with runtime.store.db:
            if runtime.store.db.execute("SELECT 1 FROM sqlite_sequence WHERE name='events'").fetchone():
                runtime.store.db.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='events'", (floor,))
            else:
                runtime.store.db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('events',?)", (floor,))

    async def signal(self, action, data):
        try:
            async with asyncio.timeout(2):
                response = await self.http.post(f"/{action}/{quote(self.line, safe='')}", json=data)
                response.raise_for_status()
            return True
        except Exception:
            return False  # Presence and read failures must not fail or repeat the user's work.

    def eligible(self, event):
        if (
            event.get("type") != "message"
            or event.get("user") != self.line
            or not event.get("line")
            or event.get("chat") not in self.allowed
            or not event.get("id")
            or not event.get("sender_lid")
            or any(
                event.get(key)
                for key in (
                    "is_group",
                    "from_me",
                    "history",
                    "offline_sync",
                    "edit_of",
                    "forwarded",
                    "forwarded_many",
                    "vote",
                    "media",
                    "card_tap",
                    "poll_answer",
                    "reaction",
                    "paused",
                    "muted",
                )
            )
        ):
            return False
        return isinstance(event.get("text"), str) and bool(event["text"].strip())

    async def receive(self, event, *, reactions=True):
        if not self.eligible(event):
            return False
        text = event["text"].strip()
        # LID is WhatsApp's stable account identity; a recycled phone number does not inherit its wallet.
        user = "wa:" + hashlib.sha256(f"{self.line}:{event['sender_lid']}".encode()).hexdigest()
        key = f"{self.line}:{event['chat']}:{event['id']}"
        if self.db.execute("SELECT 1 FROM inbox WHERE key=?", (key,)).fetchone():
            return False
        with self.db:
            self.db.execute("INSERT INTO inbox(key,state,user_id) VALUES(?, 'processing', ?)", (key, user))
            self.db.execute(
                "INSERT INTO routes(user_id,chat) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET chat=excluded.chat",
                (user, event["chat"]),
            )
        # A crashed processing entry is not replayed automatically. The user can query tasks/receipts.
        self.active_inputs.add(key)
        turn = WhatsAppTurn(self, event, reactions)
        chat = asyncio.create_task(self.runtime.chat(user, text, interaction=turn))
        recorded = True
        try:
            delay = float(self.runtime.config.env.get("AGENT_WHATSAPP_PROGRESS_SECONDS") or 15)
            done, _ = await asyncio.wait({chat}, timeout=delay)
            if not done and turn.typing:
                try:
                    self.runtime.store.notice(
                        user, "still checking. no confirmed result yet"
                    )
                except (sqlite3.Error, OSError):
                    pass
            await chat
        except Exception as exc:
            try:
                self.runtime.store.notice(user, chat_error_message(exc))
            except (sqlite3.Error, OSError):
                recorded = False
        finally:
            if not chat.done():
                chat.cancel()
                await asyncio.gather(chat, return_exceptions=True)
            self.active_inputs.discard(key)
            await turn.finish()
        try:
            with self.db:
                self.db.execute(
                    "UPDATE inbox SET state=? WHERE key=?", ("recorded" if recorded else "error_pending", key)
                )
        except (sqlite3.Error, OSError):
            pass  # The committed processing entry will be recovered without replaying the request.
        return True

    def recover_interrupted(self):
        """Explain interrupted inputs once. Never replay their tools or payments."""
        rows = self.db.execute("SELECT key,user_id FROM inbox WHERE state IN ('processing','error_pending')").fetchall()
        for key, user in rows:
            if key in self.active_inputs:
                continue
            if not user:
                matches = [
                    u
                    for u, chat in self.db.execute("SELECT user_id,chat FROM routes")
                    if key.startswith(f"{self.line}:{chat}:")
                ]
                if len(matches) != 1:
                    continue
                user = matches[0]
            identity = hashlib.sha256(key.encode()).hexdigest()
            existing = self.runtime.store.db.execute(
                "SELECT id FROM events WHERE user_id=? AND kind='chat_message' "
                "AND json_extract(data,'$.interrupted_input')=? LIMIT 1",
                (user, identity),
            ).fetchone()
            if not existing:
                self.runtime.store.event(
                    user,
                    "chat_message",
                    {
                        "role": "assistant",
                        "content": "Your previous request was interrupted before I could reply. "
                        "I will not repeat a payment automatically. Ask me to check the existing task "
                        "or receipt before retrying.",
                        "interrupted_input": identity,
                    },
                    source="system",
                )
            with self.db:
                self.db.execute("UPDATE inbox SET state='interrupted',user_id=? WHERE key=?", (user, key))

    async def deliver_once(self):
        self.recover_interrupted()
        for user, chat, cursor in self.db.execute("SELECT user_id,chat,cursor FROM routes").fetchall():
            if chat not in self.allowed:
                continue
            for event in self.runtime.store.events(user, after=cursor):
                reaction = event["kind"] == "chat_reaction"
                written = event["kind"] == "chat_message" and event["data"].get("role") == "assistant"
                if (
                    event["data"].get("setupStage") not in {None, "ready"}
                    and self.runtime.lending.read(user).get("setup", {}).get("notifyMode") == "ready_only"
                ):
                    with self.db:
                        self.db.execute("UPDATE routes SET cursor=? WHERE user_id=?", (event["id"], user))
                    continue
                if not reaction and not written:
                    with self.db:
                        self.db.execute("UPDATE routes SET cursor=? WHERE user_id=?", (event["id"], user))
                    continue
                path, payload = "send", {"to": chat, "text": event["data"].get("content", "")}
                if reaction:
                    original = self.runtime.store.events(user, after=event["data"]["message_event_id"] - 1, limit=1)
                    channel = original[0]["data"].get("channel", {}) if original else {}
                    if channel.get("name") != "whatsapp" or channel.get("chat") != chat:
                        with self.db:
                            self.db.execute("UPDATE routes SET cursor=? WHERE user_id=?", (event["id"], user))
                        continue
                    path, payload = "react", {
                        "chat": chat, "id": channel["message_id"], "sender": channel["sender"],
                        "from_me": False, "emoji": event["data"]["emoji_reaction"],
                    }
                existing = self.db.execute("SELECT 1 FROM deliveries WHERE event_id=?", (event["id"],)).fetchone()
                if not existing:
                    # Record admission before HTTP. A timeout may follow delivery; never blindly send twice.
                    with self.db:
                        self.db.execute("INSERT INTO deliveries VALUES(?, 'unknown', NULL)", (event["id"],))
                    try:
                        if written:
                            # Once there are words to send, show typing briefly. A
                            # completed reaction-only turn never enters this path.
                            await self.signal("typing", {"to": chat, "on": True})
                            await asyncio.sleep(min(1.5, 0.25 + len(payload["text"]) / 400))
                        response = await self.http.post(
                            f"/{path}/{quote(self.line, safe='')}", json=payload
                        )
                        response.raise_for_status()
                        receipt = response.json()
                        with self.db:
                            self.db.execute(
                                "UPDATE deliveries SET state='accepted',provider_id=? WHERE event_id=?",
                                (str(receipt.get("id") or ""), event["id"]),
                            )
                    except Exception:
                        pass  # An uncertain WhatsApp send never retries a payment or its own message.
                    finally:
                        if written:
                            # Sending a progress bubble clears sidecar presence.
                            # Restore it if the main turn is still doing tool work.
                            await self.signal("typing", {"to": chat, "on": chat in self.working_chats})
                with self.db:
                    self.db.execute("UPDATE routes SET cursor=? WHERE user_id=?", (event["id"], user))

    async def run(self):
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import ResponseError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        while True:
            try:
                await self.redis.xgroup_create(self.stream, self.group, id="$", mkstream=True)
                break
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
                break
            except (RedisConnectionError, RedisTimeoutError):
                await asyncio.sleep(5)
        pending = True

        async def outbound():
            while True:
                try:
                    await self.deliver_once()
                except (sqlite3.Error, OSError):
                    pass  # Keep the service alive; persist the deferred notice once storage recovers.
                await asyncio.sleep(1)

        sender = asyncio.create_task(outbound())
        try:
            while True:
                try:
                    batches = await self.redis.xreadgroup(
                        self.group, "agent", {self.stream: "0" if pending else ">"}, count=10, block=1000
                    )
                    if not any(entries for _stream, entries in batches):
                        pending = False
                    decoded = [
                        (entry_id, json.loads(fields.get("data", "{}")))
                        for _stream, entries in batches for entry_id, fields in entries
                    ]
                    newest = {}
                    for entry_id, event in decoded:
                        if self.eligible(event) and not self.db.execute(
                            "SELECT 1 FROM inbox WHERE key=?",
                            (f"{self.line}:{event['chat']}:{event['id']}",),
                        ).fetchone():
                            newest[(event["chat"], event["sender_lid"])] = entry_id
                    for entry_id, event in decoded:
                        try:
                            await self.receive(event, reactions=(
                                newest.get((event.get("chat"), event.get("sender_lid"))) == entry_id
                            ))
                        except (sqlite3.Error, OSError):
                            pending = True
                            await asyncio.sleep(5)
                            continue  # Intake was not durable; leave the Redis message unacknowledged.
                        await self.redis.xack(self.stream, self.group, entry_id)
                except (RedisConnectionError, RedisTimeoutError):
                    pending = True
                    await asyncio.sleep(5)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def close(self):
        await self.redis.aclose()
        await self.http.aclose()
        self.db.close()


async def run_whatsapp(config):
    from agent.runtime import Runtime

    runtime = Runtime(config)
    adapter = None
    services = []
    try:
        adapter = IraWhatsApp(runtime)
        runtime.recover(strict=False)
        services = [asyncio.create_task(runtime.worker()), asyncio.create_task(adapter.run())]
        if config.env.get("AGENT_WALLET_PUBLIC_URL"):
            import uvicorn

            from agent.wallet_http import create_wallet_app

            server = uvicorn.Server(
                uvicorn.Config(
                    create_wallet_app(runtime),
                    host="127.0.0.1",
                    port=int(config.env.get("AGENT_WALLET_PORT") or 8765),
                    access_log=False,
                    server_header=False,
                )
            )
            services.append(asyncio.create_task(server.serve()))
        done, _ = await asyncio.wait(services, return_when=asyncio.FIRST_COMPLETED)
        for service in done:
            service.result()
    finally:
        for service in services:
            service.cancel()
        await asyncio.gather(*services, return_exceptions=True)
        if adapter:
            await adapter.close()
        await runtime.close()
