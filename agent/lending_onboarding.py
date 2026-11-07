"""Chat-reviewed setup using fixed artifacts and the existing per-transaction journal."""

import asyncio
import json
import re
import time

from agent.approval_language import approval_in_context, is_approval, normalize_approval
from agent.setup_progress import UPDATE_SECONDS, setup_progress


class LendingOnboarding:
    def __init__(self, lending):
        self.lending = lending
        self.last_scan = 0

    async def invoke(self, user, confirmation=None):
        service = self.lending
        wallet = service.runtime.wallets.agent_wallet(user)
        if wallet["kind"] != "local":
            raise ValueError("Setup needs your existing encrypted agent wallet.")
        payload = {
            "directory": str(service.setup_directory(user).resolve()),
            "owner": wallet["address"],
            "user": user,
            "confirmation": confirmation,
        }
        if confirmation:
            state = service.read(user).get("setup", {})
            if (
                not state.get("approvalEvent")
                or state.get("reviewHash") != confirmation
                or state.get("owner") != wallet["address"]
                or state.get("status") not in {"deploying", "paused", "ready", "verification_pending"}
            ):
                raise ValueError("Setup has no matching human approval.")
            payload["reconcileOnly"] = state["status"] in {"paused", "ready", "verification_pending"}
            if state["status"] not in {"ready", "verification_pending"}:
                row = service.store.db.execute(
                    "SELECT secret FROM wallets WHERE user_id=? AND kind='local'", (user,)
                ).fetchone()
                if not row:
                    raise ValueError("The approved agent wallet is unavailable.")
                payload["key"] = "0x" + service.vault.open(row[0], f"wallet:{user}").removeprefix("0x")
        process = await asyncio.create_subprocess_exec(
            "node",
            str(service.code_root / "scripts/part4/setup.mjs"),
            cwd=service.code_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(90):
                output, _ = await process.communicate(json.dumps(payload).encode())
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            payload.pop("key", None)
        try:
            result = json.loads(output.decode().strip().splitlines()[-1])
            if process.returncode or "error" in result:
                raise ValueError
            return result
        except (ValueError, IndexError, UnicodeError):
            raise ValueError(
                "Setup could not advance. Existing transactions remain saved for reconciliation; "
                "no replacement transaction was created."
            ) from None

    async def status(self, user):
        service = self.lending
        owner = service.binding(user)
        if service.mode != "testnet":
            return {"status": "mock", "note": "Local fixture only; no contract setup needed."}
        async with service.lock(user):
            state = service.read(user)
            setup = state.get("setup", {})
            path = service.bindings_path(user)
            if path.exists() and setup.get("status") != "verification_pending":
                manifest = json.loads(path.read_text()).get(user, {})
                if manifest.get("owner", "").lower() != owner.lower():
                    raise ValueError("The strategy setup belongs to a different wallet.")
                return {"status": "ready", "owner": owner, **setup_progress({**setup, "status": "ready"}, time.time())}
            return {
                "status": setup.get("status", "needs_setup"),
                "owner": owner,
                "lastTransaction": setup.get("transaction"),
                "progress": setup.get("progress"),
                "error": setup.get("error"),
                **setup_progress(setup, time.time()),
            }

    async def prepare(self, user, *, repeat_review=False):
        service = self.lending
        current = await self.status(user)
        if current["status"] in {"ready", "mock", "deploying", "paused", "verification_pending"}:
            return current
        if current["status"] == "awaiting_approval" and not repeat_review:
            return current
        async with service.lock(user):
            state = service.read(user)
            prior = state.get("setup", {})
            # Recheck after waiting for the lock. A repeated tool call must not
            # replace approval or renew the review merely to answer a question.
            if prior.get("status") in {"deploying", "paused", "ready", "verification_pending"} or (
                prior.get("status") == "awaiting_approval" and not repeat_review
            ):
                return {"status": prior["status"], **setup_progress(prior, time.time())}
            result = await self.invoke(user)
            if not re.fullmatch(r"0x[0-9a-f]{64}", result.get("reviewHash", "")):
                raise ValueError("Setup returned no valid review.")
            state["setup"] = {
                "status": "awaiting_approval",
                "reviewHash": result["reviewHash"],
                "owner": result["owner"],
                "approvalEvent": None,
                "createdAt": time.time(),
                "review": self.review(result),
                "notifyMode": prior.get("notifyMode", "progress"),
            }
            service.save(user, state)
            return {"status": "awaiting_approval", "review": state["setup"]["review"]}

    @staticmethod
    def review(result):
        lines = [
            "Before I can manage your test tokens, I need to set up your strategy contracts.",
            f"Owner: {result['owner']} (your encrypted agent wallet).",
            "Create one mock lending pool and one strategy wallet on Ethereum Sepolia and Base Sepolia, "
            "then pair the strategy wallets. Six transactions in total.",
            "Setup spends test ETH for gas only. Execution gas is capped at 0.01 ETH per transaction; "
            "Base also charges an L1 data fee. No CCIP-BnM or LINK will be moved during setup.",
        ]
        for chain in result["chains"]:
            lines.append(f"Chain {chain['chainId']}: pool {chain['pool']}; strategy wallet {chain['wallet']}.")
        lines.extend(
            [
                "These are test pools with controlled rates and no earned interest. "
                "CRE runs locally through this agent.",
                "After setup, I will show a separate management plan before depositing or moving your tokens.",
                "Say 'go ahead' or 'approve' to deploy this setup. Nothing has been sent yet.",
                f"Setup reference: {result['reviewHash']}",
            ]
        )
        return "\n".join(lines)

    def latest_review(self, user, event_id):
        rows = self.lending.store.db.execute(
            "SELECT data FROM events WHERE user_id=? AND id<? AND kind='chat_message' "
            "AND source!='user' ORDER BY id DESC",
            (user, event_id),
        )
        for row in rows:
            msg = json.loads(row["data"])
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            matches = set(re.findall(r"Setup reference: (0x[0-9a-f]{64})", content))
            if matches:
                return next(iter(matches)) if len(matches) == 1 else None
            if "Plan reference:" in content or re.search(r"approve (?:lending |[a-f0-9]{12}\b)", content, re.I):
                return None
        return None

    async def handle(self, user, text, event_id):
        service = self.lending
        if service.mode != "testnet" or not event_id:
            return None
        # A short progress question has a deterministic answer. Leave broader
        # strategy questions to the model, and management status to the plan.
        question = " ".join(text.lower().replace("’", "'").split()).rstrip("?.!")
        if re.fullmatch(
            r"(?:please )?(?:(?:what(?:'s| is) (?:the |my |current )?)?(?:setup )?status|"
            r"(?:any |setup )?updates?|is (?:it|the setup|my setup) (?:ready|done)(?: yet)?)",
            question,
        ):
            state = service.read(user)
            if state.get("setup") and ("setup" in question or not state.get("plan")):
                service.authenticate(user, event_id)
                return (await self.status(user))["message"]
        if re.fullmatch(
            r"(?:please\s+)?stop updating me(?:[,. ]+(?:just|only) update me when (?:the )?(?:whole )?"
            r"setup is (?:ready|done|complete))?[.!]?",
            text.strip(),
            re.I,
        ):
            service.authenticate(user, event_id)
            return await self.notifications(user, ready_only=True)
        # Deployment wording belongs only to setup, never token-payment approval.
        deploy = re.fullmatch(
            r"/?(?:(?:ok(?:ay)?|cool|alright|yes)[, ]+)?(?:please\s+)?deploy"
            r"(?:\s+(?:it|this|the setup|this setup|the contracts|the transaction))?"
            r"(?:[, ]+(?:please|now|let's go))?[.!]?",
            normalize_approval(text),
            re.I,
        )
        accepted = approval_in_context(service.store, user, event_id, text)
        if not accepted and is_approval(text):
            # A status reply used to strand the review. Only resume a clear
            # go-ahead after a setup approval reminder, not 'yes' to another question.
            row = service.store.db.execute(
                "SELECT data FROM events WHERE user_id=? AND id<? AND kind='chat_message' "
                "AND source!='user' ORDER BY id DESC LIMIT 1",
                (user, event_id),
            ).fetchone()
            message = json.loads(row[0]) if row else {}
            content = message.get("content", "")
            accepted = bool(
                re.search(r"\bgo ahead\b", text, re.I)
                and message.get("role") == "assistant"
                and re.search(r"\bsetup\b.*\bawaiting approval\b", content, re.I | re.S)
                and "?" not in content
            )
        if not accepted and not deploy:
            return None
        reference = self.latest_review(user, event_id)
        if not reference:
            return None
        service.authenticate(user, event_id)
        row = service.store.db.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
        if json.loads(row[0]).get("content", "").strip() != text.strip():
            raise ValueError("Approval must be the user's actual incoming message.")
        async with service.lock(user):
            state = service.read(user)
            setup = state.get("setup", {})
            if setup.get("reviewHash") != reference or setup.get("owner") != service.binding(user):
                raise ValueError("That setup review changed. Ask me to show the current setup first.")
            if setup.get("approvalEvent"):
                return "That setup is already approved. " + setup_progress(setup, time.time())["message"]
            if setup.get("status") != "awaiting_approval":
                raise ValueError("That setup review is cancelled. Ask me to show a new review first.")
            if time.time() - setup["createdAt"] > 3600:
                raise ValueError("That setup review expired. Ask me to show it again before approving.")
            setup.update(status="deploying", approvalEvent=event_id, stageStartedAt=time.time(), stage="creating")
            service.save(user, state)
            self.last_scan = 0  # Admit the next worker pass immediately.
        updates = (
            "I will message you only when the whole setup is ready."
            if setup.get("notifyMode") == "ready_only"
            else "I will send progress updates here."
        )
        return (
            "Setup approved. I am creating your test pools and strategy wallets on both networks, "
            "then connecting them. "
            "After that, I will wait for the networks to fully confirm the setup. "
            f"You do not need to do anything else. {updates} No tokens are being deposited."
        )

    async def notifications(self, user, *, ready_only):
        service = self.lending
        async with service.lock(user):
            state = service.read(user)
            setup = state.get("setup")
            if not setup:
                raise ValueError("No setup is active yet.")
            setup["notifyMode"] = "ready_only" if ready_only else "progress"
            service.save(user, state)
        preference = (
            "Progress updates are off. I will message you only when the whole setup is ready. "
            if ready_only
            else "Progress updates are on. I will update you as setup progresses and when it is ready. "
        )
        if setup.get("status") == "deploying" and setup.get("approvalEvent"):
            return preference + "Setup will keep running; you can still ask for its status."
        return preference + setup_progress(setup, time.time())["message"]

    async def control(self, user, *, pause):
        service = self.lending
        if service.mode != "testnet":
            return None
        async with service.lock(user):
            state = service.read(user)
            service.binding(user, state)
            setup = state.get("setup", {})
            if setup.get("status") not in {"awaiting_approval", "deploying", "paused"}:
                return None
            if pause:
                # A paused review cannot be approved until shown again.
                setup["status"] = "paused" if setup.get("approvalEvent") else "cancelled"
            elif not setup.get("approvalEvent"):
                raise ValueError("Review and approve the setup before starting it.")
            else:
                setup["status"] = "deploying"
            service.save(user, state)
            return {"status": setup["status"], "note": "Existing submitted transactions remain tracked."}

    async def tick(self):
        service = self.lending
        if service.mode != "testnet" or time.monotonic() - self.last_scan < 3:
            return
        self.last_scan = time.monotonic()
        for row in service.store.db.execute("SELECT user_id FROM lending_private").fetchall():
            user = row[0]
            async with service.lock(user):
                state = service.read(user)
                setup = state.get("setup", {})
                background = setup.get("status") in {"ready", "verification_pending"}
                if setup.get("status") not in {"deploying", "paused", "ready", "verification_pending"} or not setup.get(
                    "approvalEvent"
                ):
                    continue
                if setup.get("status") == "ready" and setup.get("finality", "finalized") in {
                    "finalized",
                    "not_required",
                }:
                    continue
                if background and time.time() - setup.get("lastCheckedAt", 0) < 30:
                    continue
                try:
                    if setup["owner"] != service.binding(user):
                        raise ValueError("Wallet changed")
                    result = await self.invoke(user, setup["reviewHash"])
                    if result.get("status") == "complete":
                        # Dedicated testnet bindings unlock after canonical mined receipts.
                        manifest = json.loads(service.bindings_path(user).read_text()).get(user, {})
                        if manifest.get("owner") != setup["owner"]:
                            raise ValueError("Invalid completed binding")
                        setup.update(
                            status="ready",
                            error=None,
                            progress=result.get("transactions", setup.get("progress")),
                            finality=result.get("finality", "finalized"),
                        )
                    elif result.get("status") == "paused":
                        if result.get("transactions") is not None:
                            setup["progress"] = result["transactions"]
                        setup["error"] = None
                    elif result.get("status") == "pending" and result.get("hash"):
                        setup.update(transaction={k: result[k] for k in ("action", "hash")}, error=None)
                        if result.get("transactions") is not None:
                            setup["progress"] = result["transactions"]
                    else:
                        raise ValueError("Unknown setup result")
                    if background and result.get("status") != "complete":
                        setup["status"] = "verification_pending"
                    setup["lastCheckedAt"] = time.time()
                except (ValueError, OSError, TimeoutError):
                    setup["error"] = "Setup dependency or receipt check pending"
                    if background:
                        setup["status"] = "verification_pending"
                now = time.time()
                view = setup_progress(setup, now)
                if setup.get("stage") != view["stage"]:
                    setup.update(stage=view["stage"], stageStartedAt=now)
                    view = setup_progress(setup, now)
                fingerprint = [view["stage"], view["succeeded"], view["settled"]]
                changed = setup.get("noticeFingerprint") != fingerprint
                reminder = (
                    view["stage"] in {"creating", "network_confirmation", "checking_problem"}
                    and now - setup.get("lastNoticeAt", 0) >= UPDATE_SECONDS
                )
                message = view["message"] if changed or reminder else None
                if background:
                    message = None  # Settlement monitoring is silent after the ready notification.
                if setup.get("notifyMode") == "ready_only" and view["stage"] != "ready":
                    message = None
                if message:
                    setup.update(noticeFingerprint=fingerprint, lastNoticeAt=now)
                service.save(user, state)
                if message:
                    service.store.event(
                        user,
                        "chat_message",
                        {
                            "role": "assistant",
                            "content": message,
                            "setupStage": view["stage"],
                        },
                    )
