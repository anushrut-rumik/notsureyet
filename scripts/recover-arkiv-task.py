"""Recover one existing task into a new Ira runtime. Copies no signing keys or payment journals."""

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path

from agent.arkiv import ArkivError
from agent.config import Config
from agent.runtime import Runtime


class NoInference:
    async def complete(self, *args):
        raise RuntimeError("This recovery demonstration cannot execute model tools")

    async def close(self):
        pass


async def recover(source_root, destination, task_id):
    source = Config(source_root)
    registry = json.loads((source.data_dir / "arkiv-scopes.json").read_text())
    instance = registry["tasks"][task_id]
    scope = registry["scopes"][instance]
    if not (source.data_dir / "vault.key").exists():
        raise ValueError("Restore the authorized encryption key before recovery")
    if destination.exists():
        raise ValueError("Recovery destination must not exist; an existing database will never be deleted")
    destination.mkdir(parents=True, mode=0o700)
    shutil.copyfile(source.root / "inference.toml", destination / "inference.toml")
    data = destination / ".local"
    data.mkdir(mode=0o700)
    shutil.copyfile(source.data_dir / "vault.key", data / "vault.key")
    (data / "vault.key").chmod(0o600)
    selected = {"scopes": {instance: scope}, "tasks": {task_id: instance}}
    (data / "arkiv-scopes.json").write_text(json.dumps(selected))
    (data / "arkiv-scopes.json").chmod(0o600)
    config = Config(
        destination,
        environ={
            "AGENT_TASK_BACKEND": "arkiv",
            "AGENT_ARKIV_ACCESS_KEY": source.env.get("AGENT_ARKIV_ACCESS_KEY") or "",
        },
    )
    runtime = Runtime(config, NoInference())
    bridge = runtime.store.bridge
    invoke = bridge._invoke
    blocked = []

    def reads_only(scope, command, **args):
        if command not in {"read", "funding"}:
            blocked.append(command)
            raise ArkivError("Clean recovery instance cannot request signing or broadcast")
        return invoke(scope, command, **args)

    bridge._invoke = reads_only
    try:
        runtime.recover()
        progress = runtime.store.progress(scope["user_id"], task_id)
        listed = runtime.store.tasks(scope["user_id"])
        assert any(task["id"] == task_id for task in listed)
        assert not list(data.glob("wallet-*.json")) and not list(data.glob("payment-*.json"))
        result = {
            "mode": "live_arkiv_read_only",
            "task_id": task_id,
            "title": progress["task"]["title"],
            "final_status": progress["task"]["status"],
            "public_state": progress["public_state"],
            "arkiv": progress["arkiv"],
            "recovered_from_empty_database": True,
            "authorized_encryption_key_restored": True,
            "wallet_signing_keys_copied": False,
            "payment_journals_copied": False,
            "inference_requests": 0,
            "payment_submissions": 0,
            "blocked_mutations": blocked,
            "event_count": len(progress["events"]),
        }
        # Avoid publishing app/chat identities from the local trusted scope descriptor.
        result["arkiv"].pop("user_id", None)
        with (destination / "recovery-report.json").open("w") as report:
            json.dump(result, report, indent=2)
        print(json.dumps(result, indent=2))
        return result
    finally:
        await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    options = parser.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(recover(options.source_root.resolve(), options.destination.resolve(), options.task_id))
    except Exception as error:
        raise SystemExit(f"Recovery stopped ({type(error).__name__}); no payment was requested") from None
