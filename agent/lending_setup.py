"""Review or execute the fixed testnet deployment from the existing encrypted agent wallet."""

import argparse
import asyncio
import json
from pathlib import Path

from agent.config import Config
from agent.runtime import Runtime


async def run(args):
    config = Config()
    config.task_backend = "sqlite"  # Setup does not acquire or mutate the running Arkiv writer.
    runtime = Runtime(config)
    try:
        user = args.user
        if not user:
            rows = runtime.store.db.execute("SELECT user_id FROM wallets WHERE kind='local'").fetchall()
            if len(rows) != 1:
                raise ValueError("Select the existing authenticated user with --user; no wallet is created.")
            user = rows[0][0]
        owner = runtime.wallets.agent_wallet(user)["address"]
        payload = {
            "directory": str(await asyncio.to_thread(Path(args.directory).resolve)),
            "owner": owner,
            "user": user,
            "confirmation": args.execute,
        }
        while True:
            if args.execute:
                row = runtime.store.db.execute(
                    "SELECT secret FROM wallets WHERE user_id=? AND kind='local'", (user,)
                ).fetchone()
                payload["key"] = "0x" + runtime.wallets.vault.open(row[0], f"wallet:{user}").removeprefix("0x")
            child = await asyncio.create_subprocess_exec(
                "node",
                "scripts/part4/setup.mjs",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await child.communicate(json.dumps(payload).encode())
            except BaseException:
                child.kill()
                await child.wait()
                raise
            finally:
                payload.pop("key", None)
            print(output.decode(), flush=True)
            if child.returncode:
                raise SystemExit(child.returncode)
            result = json.loads(output)
            if not args.execute or result.get("status") != "pending":
                break
            await asyncio.sleep(15)

    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user")
    parser.add_argument("--directory", default=".local/lending-agent-setup")
    parser.add_argument(
        "--execute", help="Exact review hash. Without this flag only public deployment review is produced."
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
