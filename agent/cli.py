import argparse
import asyncio
import contextlib
import json

from agent.arkiv import initialize, open_store
from agent.config import Config
from agent.inference import InferenceError
from agent.runtime import Runtime


async def conversation(config, user):
    runtime = Runtime(config)
    worker = asyncio.create_task(runtime.worker())
    cursor = runtime.store.db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]

    async def notices():
        nonlocal cursor
        while True:
            for event in runtime.store.events(user, after=cursor, kind="chat_message"):
                cursor = event["id"]
                if event["data"]["role"] == "assistant":
                    print("\nira> " + event["data"]["content"], flush=True)
            await asyncio.sleep(0.2)

    printer = asyncio.create_task(notices())
    print("Ira is ready. /tasks lists work; /quit exits. Unfinished tasks resume the next time you start.")
    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "you> ")
            except EOFError:
                break
            if text.strip() == "/quit":
                break
            if text.strip() == "/tasks":
                for task in runtime.store.tasks(user):
                    print(f"{task['id']}  {task['status']:12}  {task['title']}")
                continue
            if text.strip():
                try:
                    await runtime.chat(user, text)
                    await asyncio.sleep(0.25)
                except InferenceError as exc:
                    print(f"Could not answer: {exc} ({exc.code})")
                except Exception as exc:
                    print(f"Could not answer: {type(exc).__name__}. Check `agent config` and your provider settings.")
    finally:
        for job in (printer, worker):
            job.cancel()
        for job in (printer, worker):
            with contextlib.suppress(asyncio.CancelledError):
                await job
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description="A small personal agent that owns tasks and uses tools.")
    parser.add_argument("--root", help="Project directory containing inference.toml and .env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="Show inference routes and key presence; no key values")
    sub.add_parser("web3-init", help="Initialize local encryption; Arkiv uses each chat's active wallet")
    sub.add_parser("whatsapp", help="Run chat and task delivery through an existing Ira WhatsApp sidecar line")
    pairing = sub.add_parser("whatsapp-link", help="Get a pairing code for the dedicated agent phone number")
    pairing.add_argument("--phone", help="Agent number with country code; defaults to AGENT_WHATSAPP_PHONE")
    sub.add_parser("whatsapp-status", help="Check the dedicated agent phone's live link state")
    check = sub.add_parser("doctor", help="Check the live model catalog; no paid inference by default")
    check.add_argument("--live", action="store_true", help="Also make two paid tool-call requests per inference role")
    chat = sub.add_parser("chat", help="Start the agent, task worker, and terminal conversation")
    chat.add_argument("--user", default="local")
    tasks = sub.add_parser("tasks", help="Inspect persistent tasks without starting model inference")
    tasks.add_argument("--user", default="local")
    serving = sub.add_parser("serve", help="Run the local HTTP API and task worker")
    serving.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    from pathlib import Path

    config = Config(Path(args.root)) if args.root else Config()
    if args.command == "config":
        print(json.dumps(config.status(), indent=2))
        print(
            json.dumps(
                {
                    "task_backend": config.task_backend,
                    "arkiv_access_key_present": bool(config.env.get("AGENT_ARKIV_ACCESS_KEY")),
                },
                indent=2,
            )
        )
    elif args.command == "web3-init":
        print(json.dumps(initialize(config), indent=2))
    elif args.command in {"whatsapp-link", "whatsapp-status"}:
        from agent.whatsapp_setup import setup

        try:
            result = asyncio.run(
                setup(config, phone=getattr(args, "phone", None), status_only=args.command == "whatsapp-status")
            )
        except ValueError as exc:
            parser.exit(1, str(exc) + "\n")
        print(json.dumps(result, indent=2))
    elif args.command == "doctor":
        from agent.diagnostics import doctor

        report = asyncio.run(doctor(config, live=args.live))
        print(json.dumps(report, indent=2))
        if not report["ok"]:
            raise SystemExit(1)
    elif args.command == "tasks":
        store = open_store(config)
        print(json.dumps(store.tasks(args.user), indent=2))
        store.close()
    elif args.command == "chat":
        asyncio.run(conversation(config, args.user))
    elif args.command == "whatsapp":
        from agent.whatsapp import run_whatsapp

        asyncio.run(run_whatsapp(config))
    else:
        import uvicorn

        from agent.server import create_app

        uvicorn.run(create_app(config), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
