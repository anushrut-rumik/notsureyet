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

from agent.channel_updates import queue_update
from agent.lending import digest


async def execute(service, user, state, action):
    if action in {"start", "evaluate"} and state.get("setup", {}).get("status") == "verification_pending":
        raise ValueError("Setup verification is pending; new investments are paused.")
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
    path = service.bindings_path(user)
    if not path.exists():
        raise ValueError("Strategy setup is not complete. Ask me to set up token management first.")
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
        # until receipts or canonical no-send proof resolve the outcome.
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
        error = ValueError(result.get("error", "Live execution remains unresolved."))
        if result.get("failureCode") in {"RPC_UNAVAILABLE", "CRE_TIMEOUT", "CRE_AUTHENTICATION", "LIVE_CHECK_FAILED"}:
            error.code = result["failureCode"]
        raise error
    previous = {o["id"]: o["state"] for o in state.get("liveProgress", {}).get("operations", [])}
    for operation in result.get("operations", []):
        if operation["state"] == "verified_deposit":
            completed = operation["details"].get("destinationBlockTimestamp")
            if completed:
                prior = plan["policy"].get("lastMoveCompletedAt")
                plan["policy"]["lastMoveCompletedAt"] = str(max(int(prior or 0), int(completed)))
        if previous.get(operation["id"]) != operation["state"]:
            details = operation.get("details") or {}
            no_send = details.get("recoveryStatus") in {"verified-no-send", "verified-before-broadcast"} or (
                operation["state"] == "cancelled" and details.get("broadcastAttempted") is False
            )
            queue_update(service.store, user, "operation_progress", {
                "mode": "testnet", "operation_id": operation["id"], "state": operation["state"],
                "no_token_transfer_verified": no_send,
                "source_transaction_hash": details.get("sourceHash"),
                "destination_transaction_hash": details.get("destinationHash"),
                "ccip_message_id": details.get("messageId"),
                "ccip_explorer_url": (
                    "https://ccip.chain.link/#/side-drawer/msg/" + details["messageId"]
                    if details.get("messageId") else None
                ),
                "destination_deposit_verified": operation["state"] == "verified_deposit",
            })
    progress = result.get("startupProgress")
    if progress and state.get("lastStartupNotice") != digest(progress):
        queue_update(service.store, user, "startup_progress", {
            "mode": "testnet", "plan_id": plan["id"], "start_requested": True,
            "progress": progress, "management_active": result["status"] == "deposit_verified",
        })
        state["lastStartupNotice"] = digest(progress)
    if not progress and state.get("lastLiveNoticeStatus") != result["status"]:
        if result["status"] in {
            "deposit_verified", "withdrawal_verified", "funding_pending",
            "submission_unknown", "submitted", "preflight_failed",
        }:
            queue_update(service.store, user, "management_progress", {
                "mode": "testnet", "status": result["status"],
                "management_active": result["status"] == "deposit_verified" or plan["status"] == "active",
                "execution_error": (result.get("execution") or {}).get("error"),
                "broadcast_attempted": result["status"] in {"submission_unknown", "submitted"},
            })
            state["lastLiveNoticeStatus"] = result["status"]
    if result["status"] == "no_move":
        decision = result["decision"]["decision"]
        if state.get("lastDecisionNotice") != decision.get("reason"):
            queue_update(service.store, user, "management_decision", {"mode": "testnet", "decision": decision})
            state["lastDecisionNotice"] = decision.get("reason")
    state["liveProgress"] = result
    if action != "monitor":
        state["error"] = ((result.get("execution") or {}).get("error") or {}).get("code")
    elif state.get("error") in {"ValueError", "OSError", "TimeoutError", "RPC_UNAVAILABLE", "LIVE_CHECK_FAILED"}:
        # A successful refresh resolves the previous transport/check failure.
        # Execution-specific failures stay visible until a new evaluation resolves them.
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
