"""Local WhatsApp transport/ledger demo. Never connects to Redis or sends provider messages."""

import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

import httpx

from agent.config import Config
from agent.providers import Answer
from agent.runtime import Runtime
from agent.whatsapp import IraWhatsApp


class DemoModels:
    async def complete(self, role, messages, tools):
        if role == "reaction":
            return Answer('{"label":"none"}')
        return Answer("Use /lending plan, start, status, pause or withdraw in this deterministic local demo.")

    async def close(self):
        pass


class MockRedis:
    async def aclose(self):
        pass


async def demo(evaluator="cre", *, scripted=False):
    # An isolated private directory ensures this demo cannot reuse real provider credentials or wallets.
    with tempfile.TemporaryDirectory(prefix="lending-whatsapp-demo-") as directory:
        root = Path(directory)
        (root / "inference.toml").write_text(
            '[routes]\ndefault="demo"\n[endpoints.demo]\nbase_url="http://unused/v1"\nmodel="local-demo"\n'
        )
        config = Config(
            root,
            environ={
                "AGENT_TASK_BACKEND": "sqlite",
                "AGENT_LENDING_MODE": "mock",
                "AGENT_LENDING_EVALUATOR": evaluator,
                "AGENT_WHATSAPP_LINE": "line-demo",
                "AGENT_WHATSAPP_ALLOWED_CHATS": "demo@s.whatsapp.net",
                "AGENT_WHATSAPP_SECRET": "local-transport-mock-only",
                "AGENT_WHATSAPP_SIDECAR_URL": "http://local-mock",
                "AGENT_WHATSAPP_REDIS_URL": "redis://unused",
            },
        )
        runtime = Runtime(config, DemoModels())
        user = "wa:" + hashlib.sha256(b"line-demo:demo@lid").hexdigest()
        runtime.wallets.create(user)
        state = runtime.lending.read(user)
        state["ledger"]["idle"]["11155111"] = str(1000 * 10**18)
        state["ledger"]["fees"] = dict.fromkeys(("11155111", "84532"), "10000000")
        runtime.lending.save(user, state)

        def respond(request):
            if request.url.path.startswith("/send/"):
                print("ira> " + json.loads(request.content)["text"], flush=True)
            return httpx.Response(200, json={"id": "mock-only"})

        transport = IraWhatsApp(
            runtime,
            redis=MockRedis(),
            http=httpx.AsyncClient(base_url="http://local-mock", transport=httpx.MockTransport(respond)),
        )
        sequence = 0

        async def send(text):
            nonlocal sequence
            sequence += 1
            print("you> " + text, flush=True)
            await transport.receive(
                {
                    "type": "message",
                    "user": "line-demo",
                    "line": True,
                    "chat": "demo@s.whatsapp.net",
                    "sender_lid": "demo@lid",
                    "id": str(sequence),
                    "text": text,
                },
                reactions=False,
            )
            await transport.deliver_once()

        async def scheduler():
            while True:
                await runtime.lending.tick()
                await transport.deliver_once()
                await asyncio.sleep(1)

        worker = None
        print("MOCK TRANSPORT + MOCK EXECUTION. No provider, handset, onchain deposit or CCIP message.", flush=True)
        print(
            f"Evaluator: {evaluator}. Fixture explicitly funded with 1000 test tokens and 10 fee units per chain.",
            flush=True,
        )
        try:
            if scripted:
                await send(
                    "Manage my test tokens for 30 days. Keep some available and only move the rest "
                    "when the expected extra interest covers the fees."
                )
                plan = runtime.lending.read(user)["plan"]
                await send(f"approve lending {plan['id']} {plan['version']} {plan['digest'][:12]}")
                await send("/lending start")
                await runtime.lending.tick_user(user)
                await transport.deliver_once()
                print("fixture> Changing the destination mock rate from 7% to 20%; same approved limits.", flush=True)
                async with runtime.lending.lock(user):
                    state = runtime.lending.read(user)
                    state["ledger"]["rates"]["84532"] = "2000"
                    state["nextCheck"] = 0
                    runtime.lending.save(user, state)
                for _ in range(3):
                    await runtime.lending.tick_user(user)
                    await transport.deliver_once()
                await send("/lending status")
                await send("/lending pause")
                await send("/lending withdraw")
                await runtime.lending.tick_user(user)
                await transport.deliver_once()
                await send("/lending status")
            else:
                worker = asyncio.create_task(scheduler())
                print("Type /lending plan to begin. /quit stops the demo and removes its temporary fixture.")
                while True:
                    try:
                        text = await asyncio.to_thread(input, "you> ")
                    except EOFError:
                        break
                    if text.strip() == "/quit":
                        break
                    await send(text)
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            transport.db.close()
            await transport.http.aclose()
            await runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluator", choices=("cre", "test-harness"), default="cre")
    parser.add_argument("--scripted", action="store_true")
    args = parser.parse_args()
    asyncio.run(demo(args.evaluator, scripted=args.scripted))


if __name__ == "__main__":
    main()
