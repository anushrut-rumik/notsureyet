"""Broker for the approved user's existing encrypted agent wallet.

No signing capability is exposed as a model tool. The caller holds the same
per-user lending lock used for approvals, scheduling, pause and withdrawal.
"""

import asyncio
import hashlib
import json
import os
import signal
from pathlib import Path

from agent.lending import digest


async def execute(service, user, state, action):
    wallet = service.runtime.wallets.agent_wallet(user)
    if wallet["kind"] != "local":
        raise ValueError("Unattended lending requires the explicitly selected encrypted agent wallet.")
    plan = state["plan"]
    if not plan or not plan["approvalEvent"]:
        raise ValueError("Approve the exact plan from WhatsApp before live wallet execution.")
    service.binding(user, state)
    if action in {"start", "evaluate"}:
        service.require_approved(state)
        if plan["status"] not in {"approved", "awaiting_deposit", "active"}:
            raise ValueError("This policy is stopped.")
    path = service.runtime.config.env.get("AGENT_LENDING_BINDINGS")
    if not path:
        raise ValueError("Configure the verified agent-owned deployments in AGENT_LENDING_BINDINGS first.")
    bindings = json.loads(await asyncio.to_thread(Path(path).read_text))
    manifest = bindings.get(user)
    if not manifest or manifest.get("owner", "").lower() != wallet["address"].lower():
        raise ValueError("No deployment is bound to this user's approved agent wallet.")
    expected = plan.get("executionBinding")
    investing = action in {"start", "evaluate"}
    if not expected or any(
        (digest(manifest.get("seedRun")) if key == "seedHash" else manifest.get(key)) != value
        for key, value in expected.items()
        if investing or key in {"owner", "workflowId", "chains"}
    ):
        raise ValueError("Deployment or native gas/funding limits changed; a new exact plan approval is required.")
    # This flag is operator configuration, never accepted from a model or a chat argument.
    if investing and manifest.get("automaticExecution") is not True:
        raise ValueError("Enable automaticExecution in the reviewed server binding after deployment verification.")
    # Disabling investments or editing a price assumption must not prevent an
    # explicit exit. Its deployments and original native spending cap stay bound.
    if not investing:
        manifest = {**manifest, "maxTransactionGasWei": expected["maxTransactionGasWei"]}
    row = service.store.db.execute("SELECT secret FROM wallets WHERE user_id=? AND kind='local'", (user,)).fetchone()
    if not row:
        raise ValueError("The approved encrypted agent wallet is unavailable.")
    directory = service.runtime.config.data_dir / "lending-live" / hashlib.sha256(user.encode()).hexdigest()
    payload = {
        "manifest": manifest,
        "plan": plan,
        "action": action,
        "user": user,
        "directory": str(directory),
        "key": "0x" + service.vault.open(row[0], f"wallet:{user}").removeprefix("0x"),
    }
    process = await asyncio.create_subprocess_exec(
        "node",
        str(service.code_root / "scripts/part4/live.mjs"),
        cwd=service.code_root,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(360):
            output, _ = await process.communicate(json.dumps(payload).encode())
    except BaseException:
        # Stop the CRE grandchildren too. Their durable journal remains unknown
        # until receipts or finalized no-send proof resolve the outcome.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    finally:
        payload.pop("key", None)
    try:
        result = json.loads(output)
    except ValueError:
        raise ValueError("Live execution outcome unknown. Preserve journals and reconcile before retrying.") from None
    if process.returncode or "error" in result:
        raise ValueError(result.get("error", "Live execution remains unresolved."))
    previous = {o["id"]: o["state"] for o in state.get("liveProgress", {}).get("operations", [])}
    for operation in result.get("operations", []):
        if operation["state"] == "verified_deposit":
            completed = operation["details"].get("destinationBlockTimestamp")
            if completed:
                prior = plan["policy"].get("lastMoveCompletedAt")
                plan["policy"]["lastMoveCompletedAt"] = str(max(int(prior or 0), int(completed)))
        if previous.get(operation["id"]) != operation["state"]:
            service.store.notice(user, "Live testnet transfer " + operation["id"] + ": " + operation["state"])
    if state.get("lastLiveNoticeStatus") != result["status"]:
        messages = {
            "deposit_verified": "Initial testnet deposit confirmed. Approved management is active.",
            "withdrawal_verified": "Principal withdrawal confirmed to your agent wallet on the current testnets.",
            "funding_pending": "Testnet funding/deposit transaction pending; management is not active yet.",
            "submission_unknown": "Transfer submission is being verified. No destination receipt yet.",
        }
        if result["status"] in messages:
            service.store.notice(user, messages[result["status"]])
            state["lastLiveNoticeStatus"] = result["status"]
    if result["status"] == "no_move":
        service.store.notice(user, "No move: " + result["decision"]["decision"]["reason"])
    state["liveProgress"] = result
    state["error"] = None
    status = result["status"]
    if action == "start":
        plan["status"] = "active" if status == "deposit_verified" else "awaiting_deposit"
    if action == "withdraw" and status == "withdrawal_verified":
        plan["status"] = "withdrawn"
        state["withdrawRequested"] = False
    if action == "evaluate":
        if result.get("decision"):
            state["lastResult"] = {"result": result["decision"], "engine": "native-local-CRE"}
        state["nextCheck"] = int(service.clock()) + plan["settings"]["checkIntervalSeconds"]
    service.save(user, state)
    return result
