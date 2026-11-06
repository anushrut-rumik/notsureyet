"""Chat-reviewed setup using fixed artifacts and the existing per-transaction journal."""

import asyncio
import json
import re
import time

from agent.approval_language import approval_in_context


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
                or state.get("status") not in {"deploying", "paused"}
            ):
                raise ValueError("Setup has no matching human approval.")
            payload["reconcileOnly"] = state["status"] == "paused"
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
            if path.exists():
                manifest = json.loads(path.read_text()).get(user, {})
                if manifest.get("owner", "").lower() != owner.lower():
                    raise ValueError("The strategy setup belongs to a different wallet.")
                return {"status": "ready", "owner": owner, "next": "Create a lending plan and show its review."}
            return {
                "status": setup.get("status", "needs_setup"),
                "owner": owner,
                "lastTransaction": setup.get("transaction"),
                "error": setup.get("error"),
                "next": "Prepare setup for review." if not setup else "Wait for setup or approve its shown review.",
            }

    async def prepare(self, user):
        service = self.lending
        current = await self.status(user)
        if current["status"] in {"ready", "mock", "deploying", "paused"}:
            return current
        async with service.lock(user):
            result = await self.invoke(user)
            if not re.fullmatch(r"0x[0-9a-f]{64}", result.get("reviewHash", "")):
                raise ValueError("Setup returned no valid review.")
            state = service.read(user)
            prior = state.get("setup", {})
            # Do not replace an in-flight setup after waiting for the lock.
            if prior.get("status") in {"deploying", "paused"}:
                return {"status": prior["status"]}
            state["setup"] = {
                "status": "awaiting_approval",
                "reviewHash": result["reviewHash"],
                "owner": result["owner"],
                "approvalEvent": None,
                "createdAt": time.time(),
                "review": self.review(result),
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
        if service.mode != "testnet" or not event_id or not approval_in_context(service.store, user, event_id, text):
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
                return "That setup is already approved. I am tracking the existing transactions."
            if setup.get("status") != "awaiting_approval":
                raise ValueError("That setup review is cancelled. Ask me to show a new review first.")
            if time.time() - setup["createdAt"] > 3600:
                raise ValueError("That setup review expired. Ask me to show it again before approving.")
            setup.update(status="deploying", approvalEvent=event_id)
            service.save(user, state)
        return "Setup approved. I will deploy and track the strategy contracts here, then tell you when they are ready."

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
        if service.mode != "testnet" or time.monotonic() - self.last_scan < 15:
            return
        self.last_scan = time.monotonic()
        for row in service.store.db.execute("SELECT user_id FROM lending_private").fetchall():
            user = row[0]
            async with service.lock(user):
                state = service.read(user)
                setup = state.get("setup", {})
                if setup.get("status") not in {"deploying", "paused"} or not setup.get("approvalEvent"):
                    continue
                try:
                    if setup["owner"] != service.binding(user):
                        raise ValueError("Wallet changed")
                    result = await self.invoke(user, setup["reviewHash"])
                    if result.get("status") == "complete":
                        # The binding file is written only after all exact receipts have finalized.
                        manifest = json.loads(service.bindings_path(user).read_text()).get(user, {})
                        if manifest.get("owner") != setup["owner"]:
                            raise ValueError("Invalid completed binding")
                        setup.update(status="ready", error=None)
                        message = (
                            "Your strategy contracts are ready on both testnets. No tokens have been deposited yet. "
                            "Tell me how you want them managed, for example: 'Manage my test tokens for 30 days "
                            "and check every minute.' I will show the plan for your approval."
                        )
                    elif result.get("status") == "paused":
                        message = None
                    elif result.get("status") == "pending" and result.get("hash"):
                        transaction = {k: result[k] for k in ("action", "hash")}
                        message = (
                            None
                            if setup.get("transaction") == transaction
                            else (
                            f"Strategy setup: {result['action']} recorded; checking its submission and confirmation.\n"
                                f"Transaction: {result['hash']}"
                            )
                        )
                        setup.update(transaction=transaction, error=None)
                    else:
                        raise ValueError("Unknown setup result")
                except (ValueError, OSError, TimeoutError):
                    message = (
                        None
                        if setup.get("error")
                        else (
                            "Strategy setup is waiting on a dependency or transaction check. "
                            "I will reconcile the saved transactions before continuing; ask me for setup status."
                        )
                    )
                    setup["error"] = "Setup dependency or receipt check pending"
                service.save(user, state)
                if message:
                    service.store.notice(user, message)
