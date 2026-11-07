"""User-facing setup stages, shared by chat status and proactive updates."""

NETWORKS = {"11155111": "Ethereum Sepolia", "84532": "Base Sepolia"}
UPDATE_SECONDS = 120


def setup_progress(setup, now):
    status = setup.get("status", "needs_setup")
    transactions = setup.get("progress") or []
    succeeded = sum(t["state"] in {"mined", "safe", "confirmed"} for t in transactions)
    settled = sum(t["state"] == "confirmed" for t in transactions)
    stage = status
    if status == "deploying":
        stage = "network_confirmation" if succeeded == 6 else "creating"
    if (setup.get("error") and status != "ready") or status == "verification_pending":
        stage = "checking_problem"
    elapsed = max(0, int(now - setup.get("stageStartedAt", now)))
    checked = setup.get("lastCheckedAt")
    if stage == "ready":
        message = (
            "Setup is ready on both testnets.\n"
            "Your test pools and strategy wallets are created, connected, and verified on the current chain.\n"
            + (
                "Full finality is still being checked quietly in the background; it does not block this test setup.\n"
                if setup.get("finality") == "pending"
                else ""
            )
            + "Next: tell me how you want your test tokens managed. I will show a separate plan for your approval "
            "before funding or starting management."
        )
        action = "Review a management plan."
    elif stage == "network_confirmation":
        waiting = [
            name
            for chain, name in NETWORKS.items()
            if any(
                t["action"].startswith(chain + "-") and t["state"] not in {"safe", "confirmed"} for t in transactions
            )
        ]
        message = (
            "All setup transactions succeeded. Waiting for safe confirmation.\n"
            "Done: both test pools and strategy wallets are created and connected. "
            f"All 6 setup transactions succeeded; {settled}/6 are fully settled.\n"
            f"Waiting for: {', '.join(waiting) or 'the networks'} to confirm the transactions as safe to use. "
            "Safe confirmation unlocks this test setup. Full finality is checked later in the background.\n"
            "You don't need to do anything or approve again. Setup has not deposited or moved your test tokens.\n"
            "I cannot give a reliable countdown because the networks control this timing. "
            + (
                "Progress updates are off. I will message you only when the whole setup is ready."
                if setup.get("notifyMode") == "ready_only"
                else "I'll update you every 2 minutes while waiting, and tell you when setup is ready."
            )
        )
        action = "No action needed."
    elif stage == "creating":
        message = (
            f"I'm setting up your strategy: {succeeded}/6 transactions have succeeded.\n"
            "I'm creating a test pool and strategy wallet on each network, then connecting the wallets. "
            "Both networks are progressing together.\n"
            "Next: verify the successful mined transactions and mark setup ready.\n"
            "You don't need to do anything. Setup uses test ETH for fees; your test tokens stay where they are."
        )
        action = "No action needed."
    elif stage == "checking_problem":
        message = (
            "I couldn't verify the latest setup progress.\n"
            f"Last verified: {succeeded}/6 setup transactions succeeded. "
            "This does not mean those transactions failed.\n"
            "I'm checking the saved transactions again before continuing. You don't need to approve again. "
            "I'll update you when the checks recover, or every 2 minutes if this continues."
        )
        action = "No action needed while I retry verification."
        if setup.get("notifyMode") == "ready_only":
            message = message.replace(
                "I'll update you when the checks recover, or every 2 minutes if this continues.",
                "Progress updates are off. I will message you only when setup is ready.",
            )
    elif stage == "paused":
        message = (
            f"Setup is paused. Last verified: {succeeded}/6 transactions succeeded.\n"
            "I am still checking transactions already sent, but will not send new ones. "
            "Tell me to resume when you want to continue."
        )
        action = "Ask to resume setup."
    elif stage == "awaiting_approval":
        message = "Setup is waiting for your approval of the review. No setup transactions have been authorized yet."
        action = "Review the setup, then approve it."
    else:
        message = "Setup has not started. I can prepare a review explaining what will be created and its test ETH fees."
        action = "Ask to prepare setup."
    if elapsed >= 60 and stage in {"creating", "network_confirmation", "checking_problem"}:
        message += f"\nTime in this stage: {elapsed // 60} minutes."
    if checked and stage not in {"ready", "awaiting_approval"}:
        age = max(0, int(now - checked))
        message += "\nLast successful check: " + ("just now." if age < 60 else f"{age // 60} minutes ago.")
    return {
        "stage": stage,
        "succeeded": succeeded,
        "settled": settled,
        "total": 6,
        "actionRequired": action,
        "message": message,
    }
