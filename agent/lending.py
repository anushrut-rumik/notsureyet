"""Private, single-host lending orchestration. Models propose settings, never permissions.

Mock execution is opt-in and uses the shared TypeScript financial rules. Live
execution uses the approved encrypted agent owner and compatible testnet wallets.
"""

import asyncio
import fcntl
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

CHAINS = ("11155111", "84532")
NAMES = {"11155111": "Ethereum Sepolia", "84532": "Base Sepolia"}
TERMINAL = {"verified_deposit", "confirmed", "cancelled"}
UINT = r"^(0|[1-9][0-9]*)$"


class PlanSettings(BaseModel):
    """Both beginner presets and expert input pass through this exact schema."""

    model_config = ConfigDict(extra="forbid", strict=True)
    amountUnits: str = Field(pattern=UINT, max_length=78)
    reserveUnits: str = Field(pattern=UINT, max_length=78)
    days: StrictInt = Field(default=30, ge=1, le=365)
    checkIntervalSeconds: StrictInt = Field(default=3600, ge=60, le=86400)
    maxFeeMicrounits: str = Field(default="3000000", pattern=UINT, max_length=78)
    minNetBenefitMicrounits: str = Field(default="1000000", pattern=UINT, max_length=78)
    maxObservationAgeSeconds: str = Field(default="300", pattern=UINT, max_length=78)
    maxObservationSkewSeconds: str = Field(default="60", pattern=UINT, max_length=78)
    cooldownSeconds: str = Field(default="86400", pattern=UINT, max_length=78)

    @field_validator(
        "amountUnits",
        "reserveUnits",
        "maxFeeMicrounits",
        "minNetBenefitMicrounits",
        "maxObservationAgeSeconds",
        "maxObservationSkewSeconds",
        "cooldownSeconds",
    )
    @classmethod
    def uint256(cls, value, info):
        if int(value) >= 2**256 or (info.field_name in {"amountUnits", "maxObservationAgeSeconds"} and int(value) == 0):
            raise ValueError("Amount must fit uint256; principal and observation age must be positive")
        return value


def tokens(units, decimals=18):
    n = int(units)
    sign = "-" if n < 0 else ""
    whole, fraction = divmod(abs(n), 10**decimals)
    return sign + str(whole) + (("." + str(fraction).zfill(decimals).rstrip("0")) if fraction else "")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def empty_ledger():
    return {
        "chainId": CHAINS[0],
        "principalUnits": "0",
        "idle": dict.fromkeys(CHAINS, "0"),
        "fees": dict.fromkeys(CHAINS, "0"),
        "rates": {CHAINS[0]: "400", CHAINS[1]: "700"},
        "capacityUnits": str(10**24),
        "moveFeeUnits": "2000000",
        "accounting": {
            "initialDepositCostMicrounits": "0",
            "realizedWithdrawalCostMicrounits": "0",
            "priorRebalanceCostMicrounits": "0",
        },
        "returned": dict.fromkeys(CHAINS, "0"),
    }


class Lending:
    def __init__(self, runtime, *, clock=time.time):
        self.runtime, self.store, self.clock = runtime, runtime.store, clock
        self.code_root = Path(__file__).resolve().parent.parent
        self.mode = runtime.config.env.get("AGENT_LENDING_MODE", "off")
        if self.mode not in {"off", "mock", "testnet"}:
            raise ValueError("AGENT_LENDING_MODE must be off, mock or testnet")
        self.native = runtime.config.env.get("AGENT_LENDING_EVALUATOR", "cre") == "cre"
        if runtime.config.env.get("AGENT_LENDING_EVALUATOR", "cre") not in {"cre", "test-harness"}:
            raise ValueError("AGENT_LENDING_EVALUATOR must be cre or test-harness")
        self.vault = runtime.wallets.vault
        self.directory = runtime.config.data_dir / "lending-locks"
        if self.mode == "off":
            return
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.store.db.executescript("""
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS lending_private (
                user_id TEXT PRIMARY KEY, sealed TEXT NOT NULL);
        """)
        self.last_scan = 0

    @asynccontextmanager
    async def lock(self, user):
        # Kernel locks survive neither process death nor restart; no unsafe lease expiry.
        # The same lock protects manual actions, scheduler, pause and withdrawals.
        path = self.directory / (hashlib.sha256(user.encode()).hexdigest() + ".lock")
        with path.open("a+") as file:
            path.chmod(0o600)
            while True:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.03)
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def read(self, user):
        row = self.store.db.execute("SELECT sealed FROM lending_private WHERE user_id=?", (user,)).fetchone()
        if row:
            return self.vault.open(row[0], "lending:" + user)
        return {
            "mode": self.mode,
            "plan": None,
            "operations": [],
            "ledger": empty_ledger(),
            "withdrawRequested": False,
            "nextCheck": 0,
            "lastResult": None,
            "error": None,
        }

    def save(self, user, state):
        # No phone numbers, conversations or policy authority go to public Arkiv entities.
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO lending_private VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET sealed=excluded.sealed",
                (user, self.vault.seal(state, "lending:" + user)),
            )

    def binding(self, user, state=None):
        if self.mode == "off":
            raise ValueError("Lending is disabled. Configure the testnet integration or explicit local mock mode.")
        if state and state["mode"] != self.mode:
            raise ValueError("Lending mode changed. Restore the original mode and reconcile its operations first.")
        wallet = self.runtime.wallets.agent_wallet(user) if self.mode == "testnet" else self.runtime.wallets.get(user)
        if wallet["kind"] not in {"local", "metamask"}:
            raise ValueError(
                "Connect a verified wallet or create an agent wallet; a watched address cannot approve lending."
            )
        if state and state["plan"] and wallet["address"].lower() != state["plan"]["policy"]["walletAddress"].lower():
            raise ValueError("The wallet binding changed. Lending is stopped; reconcile the original wallet first.")
        return wallet["address"]

    async def rules(self, request):
        root = self.code_root
        # Fixed executable and argument vector; model input is JSON on stdin, never shell code.
        process = await asyncio.create_subprocess_exec(
            "node",
            str(root / "scripts/part4/rules.mjs"),
            cwd=root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(130):
                out, _ = await process.communicate(json.dumps(request).encode())
        except BaseException:
            process.kill()
            await process.wait()
            raise
        try:
            result = json.loads(out)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Shared financial rules are unavailable. Run npm ci and npm run lending:build.") from None
        if process.returncode or "error" in result:
            raise ValueError(result.get("error", "Financial evaluation failed"))
        return result

    async def testnet_portfolio(self, user, wallet):
        # Server-owned mapping, never a tool/model supplied manifest path.
        path = self.runtime.config.env.get("AGENT_LENDING_BINDINGS")
        if not path:
            raise ValueError(
                "Testnet dependency unavailable: configure AGENT_LENDING_BINDINGS with verified deployments."
            )
        bindings = json.loads(await asyncio.to_thread(Path(path).read_text))
        manifest = bindings.get(user)
        if not manifest or manifest.get("owner", "").lower() != wallet.lower():
            raise ValueError("No testnet deployment is bound to this authenticated user and wallet.")
        journal = (
            self.runtime.config.data_dir
            / "lending-live"
            / hashlib.sha256(user.encode()).hexdigest()
            / "operations.sqlite"
        )
        if await asyncio.to_thread(journal.exists):
            manifest = {**manifest, "operationJournal": str(journal)}
        process = await asyncio.create_subprocess_exec(
            "node",
            str(self.code_root / "scripts/part4/portfolio.mjs"),
            cwd=self.code_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(60):
                out, _ = await process.communicate(json.dumps({**manifest, "authenticatedUserId": user}).encode())
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ValueError("Testnet observations unavailable or wallet binding failed. No action admitted.")
        return json.loads(out)

    @staticmethod
    def pending(state):
        return next((o for o in state["operations"] if o["state"] not in TERMINAL), None)

    async def portfolio(self, user):
        async with self.lock(user):
            state = self.read(user)
            wallet = self.binding(user, state)
            if self.mode == "testnet":
                observed = await self.testnet_portfolio(user, wallet)
                state["testnetProgress"] = observed
                self.save(user, state)
                return observed
            return {
                "mode": "MOCK: no real tokens or transactions",
                "wallet": wallet,
                **state["ledger"],
                "tokensInTransitUnits": self.in_transit(state),
                "earnedProfitUnits": "0",
            }

    @staticmethod
    def in_transit(state):
        return str(
            sum(
                int(o["amountUnits"])
                for o in state["operations"]
                if o["kind"] == "rebalance" and o["state"] in {"submitted", "in_transit", "unknown"}
            )
        )

    async def create_plan(self, user, settings=None, *, preset_days=30):
        async with self.lock(user):
            state = self.read(user)
            wallet = self.binding(user, state)
            if self.pending(state) or int(state["ledger"]["principalUnits"]):
                raise ValueError("Pause and withdraw/reconcile the existing position before replacing its policy.")
            if state["plan"] and state["plan"]["status"] in {"active", "awaiting_deposit"}:
                raise ValueError("Pause the existing policy before creating a replacement.")
            source = CHAINS[0]
            if self.mode == "testnet":
                observed = await self.testnet_portfolio(user, wallet)
                available = int(observed["chains"][source]["ownerBnMUnits"])
                if any(int(c["principalUnits"]) or int(c["idleUnits"]) for c in observed["chains"].values()) or any(
                    o["state"] not in {"verified_deposit", "cancelled", "recovered_idle"}
                    for o in observed["operations"]
                ):
                    raise ValueError("Withdraw and reconcile the existing strategy funds before replacing its policy.")
            else:
                available = int(state["ledger"]["idle"][source])
            preset = settings is None
            if preset:
                if available == 0:
                    raise ValueError(
                        "No test CCIP-BnM is available on Ethereum Sepolia. "
                        "Fund your verified wallet first; "
                        "a chat instruction cannot supply tokens. Then ask for a plan again."
                    )
                reserve = available // 5
                settings = {"amountUnits": str(available - reserve), "reserveUnits": str(reserve), "days": preset_days}
                if self.mode == "testnet":
                    deployment = observed["chains"][source]
                    settings.update(
                        amountUnits=deployment["maxMoveUnits"],
                        reserveUnits=deployment["protectedReserveUnits"],
                        maxObservationAgeSeconds="1800",
                        maxObservationSkewSeconds="900",
                    )
            settings = PlanSettings.model_validate(settings).model_dump()
            if self.mode == "testnet" and any(
                c["maxMoveUnits"] != settings["amountUnits"] or c["protectedReserveUnits"] != settings["reserveUnits"]
                for c in observed["chains"].values()
            ):
                raise ValueError(
                    "Amount and reserve must match both reviewed strategy deployments; redeploy to change them."
                )
            if int(settings["amountUnits"]) + int(settings["reserveUnits"]) > available:
                raise ValueError("Principal plus protected reserve exceeds the observed test-token balance.")
            plan_id = uuid4().hex
            version = str(int(state["plan"]["version"]) + 1) if state["plan"] else "1"
            result = await self.rules(
                {
                    "action": "plan",
                    "settings": settings,
                    "now": str(int(self.clock())),
                    "user": user,
                    "wallet": wallet,
                    "version": version,
                    "planId": plan_id,
                }
            )
            plan = {
                "id": plan_id,
                "version": version,
                "policy": result["policy"],
                "settings": settings,
                "preset": preset,
                "executionMode": self.mode,
                "availableUnits": str(available),
                "status": "proposed",
                "approvalEvent": None,
            }
            if self.mode == "testnet":
                path = self.runtime.config.env.get("AGENT_LENDING_BINDINGS")
                bindings = json.loads(await asyncio.to_thread(Path(path).read_text))
                binding = bindings[user]
                plan["executionBinding"] = {
                    key: binding[key]
                    for key in ("owner", "workflowId", "chains", "maxTransactionGasWei", "linkFundingUnits")
                }
                plan["executionBinding"]["seedHash"] = digest(binding["seedRun"])
                plan["observedMarketRates"] = {c: data["aprBps"] for c, data in observed["chains"].items()}
            # Fingerprint includes deadline, wallet, limits, interval, preset and displayed balance.
            plan["digest"] = digest(plan)
            state["plan"] = plan
            state["withdrawRequested"] = False
            self.save(user, state)
            return {"plan": plan, "review": self.review(plan)}

    @staticmethod
    def review(plan):
        p, s = plan["policy"], plan["settings"]
        deadline = datetime.fromtimestamp(int(p["deadline"]), UTC).isoformat()
        return (
            f"{'TEST PRESET' if plan['preset'] else 'EXPERT TEST PLAN'} — CCIP-BnM only.\n"
            + (
                "MOCK balances, fees and execution; no real transactions.\n"
                if plan["executionMode"] == "mock"
                else "LIVE TESTNET: the approved encrypted agent wallet signs permitted actions automatically.\n"
            )
            + (
                "Execution binding: " + json.dumps(plan["executionBinding"]) + "\n"
                "Gas quantities are native wei; LINK funding is in 18-decimal units. "
                "Each chain needs its own idle reserve and fee funding.\n"
                if "executionBinding" in plan
                else ""
            )
            + (
                "Controlled mock APR: "
                + ", ".join(NAMES[c] + " " + tokens(rate, 2) + "%" for c, rate in plan["observedMarketRates"].items())
                + ". No interest accrues.\n"
                if "observedMarketRates" in plan
                else ""
            )
            + f"Plan {plan['id']} version {plan['version']} · review {plan['digest'][:12]}\n"
            f"Observed available: {tokens(plan['availableUnits'])} CCIP-BnM on Ethereum Sepolia.\n"
            f"Deposit: {tokens(s['amountUnits'])}; keep {tokens(s['reserveUnits'])} idle.\n"
            "Approved markets: mock-lending-sepolia (Ethereum Sepolia, 11155111) and "
            "mock-lending-base-sepolia (Base Sepolia, 84532).\n"
            f"Horizon: {s['days']} days, ending {deadline}. Check every {s['checkIntervalSeconds']} seconds.\n"
            f"Fee cap per move: {tokens(s['maxFeeMicrounits'], 6)} SIM_USD; minimum extra interest after fees: "
            f"{tokens(s['minNetBenefitMicrounits'], 6)} SIM_USD.\n"
            f"Maximum movable: {tokens(p['maxMovableUnits'])} CCIP-BnM; cooldown {s['cooldownSeconds']} seconds; "
            f"observation age limit {s['maxObservationAgeSeconds']} seconds, "
            f"skew {s['maxObservationSkewSeconds']} seconds.\n"
            "SIM_USD pricing is fictional. Rates can change. Projected returns assume unchanged rates and zero "
            "transfer downtime; they are not earned profit. The mock pools accrue no interest.\n"
            "Stop new investments on pause, expiry, missing/stale data, wallet changes, unavailable dependencies, "
            "insufficient reserves/fees or any pending transfer. Track submitted transfers even after stopping. "
            "At expiry the position stays deposited until you request withdrawal. Withdrawals return on their "
            "current testnet to your verified wallet; no bank cash-out. "
            "Routine permitted moves need no new chat approval.\n"
            "Reply approve to approve this exact plan.\n"
            f"Plan reference: approve lending {plan['id']} {plan['version']} {plan['digest'][:12]}"
        )

    def latest_review_command(self, user, event_id):
        rows = self.store.db.execute(
            "SELECT data FROM events WHERE user_id=? AND id<? AND kind='chat_message' "
            "AND source!='user' ORDER BY id DESC",
            (user, event_id),
        )
        for row in rows:
            message = json.loads(row["data"])
            if message.get("role") != "assistant":
                continue
            content = message.get("content", "")
            matches = set(re.findall(r"approve lending [a-f0-9]{32} [0-9]+ [a-f0-9]{12}", content))
            if matches:
                return next(iter(matches)) if len(matches) == 1 else None
            if re.search(r"\bapprove\s+[a-f0-9]{12}\b", content, re.I):
                return None  # The latest review was for a payment, not this plan.
        return None

    async def approve(self, user, text, event_id):
        # This method is deliberately absent from model tools. Only raw incoming command handling calls it.
        async with self.lock(user):
            state = self.read(user)
            self.binding(user, state)
            plan = state["plan"]
            row = self.store.db.execute("SELECT * FROM events WHERE id=? AND user_id=?", (event_id, user)).fetchone()
            event = json.loads(row["data"]) if row else {}
            channel = event.get("channel", {})
            if (
                not row
                or row["source"] != "user"
                or row["kind"] != "chat_message"
                or (channel.get("name") != "whatsapp" or not channel.get("verified_transport"))
                or event.get("content", "").strip() != text
            ):
                raise ValueError("Lending approval requires an explicit message from the authenticated WhatsApp user.")
            selected = (
                self.latest_review_command(user, event_id)
                if re.fullmatch(r"/?approve(?:\s+(?:it|this))?[.!]?", text, re.I)
                else text
            )
            if not plan or selected != f"approve lending {plan['id']} {plan['version']} {plan['digest'][:12]}":
                raise ValueError("Approval does not match the current exact plan and version. Read the current review.")
            if int(self.clock()) >= int(plan["policy"]["deadline"]):
                raise ValueError("This plan has expired. Create a new plan and review its deadline.")
            if plan["approvalEvent"]:
                return "This exact plan is already approved. No operation was repeated."
            plan["approvalEvent"] = event_id
            plan["status"] = "approved"
            plan["policy"]["approvalId"] = f"whatsapp-event:{event_id}:{plan['digest']}"
            plan["policy"]["approvedAt"] = str(int(self.clock()))
            self.save(user, state)
            return (
                "Exact plan approved. No funds were created or moved. Use /lending start to verify funding and deposit."
            )

    def require_approved(self, state):
        plan = state["plan"]
        if not plan or not plan["approvalEvent"]:
            raise ValueError("Review and explicitly approve the exact plan from WhatsApp first.")
        if int(self.clock()) >= int(plan["policy"]["deadline"]):
            raise ValueError("The absolute plan deadline has passed. Withdrawal remains available.")
        return plan

    async def start(self, user):
        async with self.lock(user):
            state = self.read(user)
            self.binding(user, state)
            plan = self.require_approved(state)
            if self.mode == "testnet":
                from agent.lending_live import execute

                if plan["status"] == "active":
                    return {"status": "Already managing this exact approved plan."}
                if plan["status"] == "approved":
                    plan["status"] = "awaiting_deposit"
                    self.save(user, state)  # Resume an authorized start even if its first RPC response is lost.
                return await execute(self, user, state, "start")
            if self.pending(state):
                return {"status": "Pending operation is being tracked; no duplicate deposit or move."}
            if plan["status"] in {"paused", "withdrawn", "withdrawing", "expired"}:
                raise ValueError("This policy is stopped. Create and approve a new plan to invest again.")
            if plan["status"] == "active":
                return {"status": "Already managing this exact approved plan."}
            ledger = state["ledger"]
            if int(ledger["principalUnits"]):
                raise ValueError("An existing position must be reconciled before initial deposit.")
            amount, reserve = int(plan["settings"]["amountUnits"]), int(plan["settings"]["reserveUnits"])
            if int(ledger["idle"][CHAINS[0]]) < amount + reserve:
                raise ValueError("Funding is missing. Deposit is disabled until principal and reserve are available.")
            op = self.operation(state, "deposit", str(amount), CHAINS[0])
            plan["status"] = "awaiting_deposit"
            self.save(user, state)  # Prepared is durable before any submission.
            self.submit_mock(user, state, op)
            return {
                "operation": self.public_operation(op),
                "message": "MOCK initial deposit submitted; not confirmed yet.",
            }

    def operation(self, state, kind, amount, chain, *, operation_id=None):
        if self.pending(state):
            raise ValueError("An unresolved operation blocks a new submission.")
        p = state["plan"]
        op = {
            "id": operation_id or uuid4().hex,
            "kind": kind,
            "state": "prepared",
            "amountUnits": amount,
            "sourceChain": chain,
            "destinationChain": CHAINS[1] if chain == CHAINS[0] else CHAINS[0],
            "planId": p["id"],
            "version": p["version"],
            "createdAt": int(self.clock()),
            "submittedAt": None,
            "confirmedAt": None,
            "receipt": None,
        }
        state["operations"].append(op)
        return op

    def submit_mock(self, user, state, op):
        if self.mode != "mock" or op["state"] != "prepared":
            raise ValueError("No permitted mock submission")
        p = state["plan"]
        if op["planId"] != p["id"] or op["version"] != p["version"]:
            raise ValueError("Policy changed before submission")
        if op["kind"] != "withdraw":
            self.require_approved(state)
            expected = "awaiting_deposit" if op["kind"] == "deposit" else "active"
            if p["status"] != expected:
                raise ValueError("The policy stopped before submission")
        ledger = state["ledger"]
        if op["kind"] == "rebalance":
            if ledger["principalUnits"] != op["amountUnits"]:
                raise ValueError("Principal changed before submission")
            fee = int(ledger["moveFeeUnits"])
            if int(ledger["fees"][op["sourceChain"]]) < fee:
                raise ValueError("Mock fee-token funding disappeared")
            ledger["fees"][op["sourceChain"]] = str(int(ledger["fees"][op["sourceChain"]]) - fee)
            ledger["principalUnits"] = "0"
            ledger["accounting"]["priorRebalanceCostMicrounits"] = str(
                int(ledger["accounting"]["priorRebalanceCostMicrounits"]) + fee
            )
            op["feeToken"] = {
                "assetId": "SIM_FEE_ASSET",
                "chainId": op["sourceChain"],
                "decimals": 6,
                "quantityUnits": str(fee),
                "simulated": True,
            }
        op["state"], op["submittedAt"] = "submitted", int(self.clock())
        # Ledger effects + submission state commit atomically. Real network effects must use an unknown-outcome journal.
        self.save(user, state)
        self.store.notice(
            user,
            "MOCK: Transfer submitted."
            if op["kind"] == "rebalance"
            else f"MOCK: {op['kind'].capitalize()} submitted; receipt not yet confirmed.",
        )

    def poll_mock(self, user, state):
        op = self.pending(state)
        if not op or op["state"] in {"prepared", "unknown"}:
            return
        ledger, p = state["ledger"], state["plan"]
        amount, chain = int(op["amountUnits"]), op["sourceChain"]
        if op["kind"] == "rebalance" and op["state"] == "submitted":
            op["state"] = "in_transit"
            self.save(user, state)
            self.store.notice(user, "MOCK: Still in transit. Destination deposit is not confirmed.")
            return
        if op["kind"] == "deposit":
            reserve = int(p["settings"]["reserveUnits"])
            if int(ledger["idle"][chain]) < amount + reserve:
                op["state"] = "unknown"
                self.save(user, state)
                return
            ledger["idle"][chain] = str(int(ledger["idle"][chain]) - amount)
            ledger["principalUnits"], ledger["chainId"] = str(amount), chain
            op["state"] = "verified_deposit"
            if p["status"] == "awaiting_deposit":
                p["status"] = "active" if int(self.clock()) < int(p["policy"]["deadline"]) else "expired"
                state["nextCheck"] = int(self.clock())
        elif op["kind"] == "rebalance":
            ledger["principalUnits"], ledger["chainId"] = str(amount), op["destinationChain"]
            p["policy"]["lastMoveCompletedAt"] = str(int(self.clock()))
            op["state"] = "verified_deposit"
        elif op["kind"] == "withdraw":
            # All currently available managed assets return on their current chains.
            amounts = dict(ledger["idle"])
            amounts[ledger["chainId"]] = str(int(amounts[ledger["chainId"]]) + int(ledger["principalUnits"]))
            for c in CHAINS:
                ledger["returned"][c] = str(int(ledger["returned"][c]) + int(amounts[c]))
                ledger["idle"][c] = "0"
            ledger["principalUnits"] = "0"
            op["returnedByChain"] = amounts
            op["state"] = "confirmed"
            p["status"] = "withdrawn"
            state["withdrawRequested"] = False
        op["confirmedAt"] = int(self.clock())
        op["receipt"] = {
            "kind": "local-ledger-receipt",
            "simulated": True,
            "operationId": op["id"],
            "recipient": p["policy"]["walletAddress"] if op["kind"] == "withdraw" else None,
        }
        self.save(user, state)
        message = (
            "Withdrawal receipt confirmed on the listed current testnets."
            if op["kind"] == "withdraw"
            else (f"Deposit confirmed on {NAMES[ledger['chainId']]}. No interest has accrued.")
        )
        self.store.notice(user, "MOCK: " + message)

    async def evaluate(self, user, *, before_submit=None):
        # Preparation and submission use separate lock admissions so pause can cancel a prepared operation.
        async with self.lock(user):
            state = self.read(user)
            self.binding(user, state)
            p = self.require_approved(state)
            if p["status"] != "active" or self.pending(state):
                return {"status": "No new move: stopped policy or unresolved operation."}
            if self.mode == "testnet":
                from agent.lending_live import execute

                return await execute(self, user, state, "evaluate")
            run_id, op_id = uuid4().hex, uuid4().hex
            evaluated = await self.rules(
                {
                    "action": "evaluate",
                    "policy": p["policy"],
                    "ledger": state["ledger"],
                    "now": str(int(self.clock())),
                    "runId": run_id,
                    "operationId": op_id,
                    "binding": "wallet:" + digest(p["policy"]["walletAddress"]),
                    "native": self.native,
                }
            )
            state["lastResult"] = evaluated
            state["error"] = None
            state["nextCheck"] = int(self.clock()) + p["settings"]["checkIntervalSeconds"]
            decision = evaluated["result"]["decision"]
            op = None
            if decision["action"] == "move":
                op = self.operation(
                    state,
                    "rebalance",
                    state["ledger"]["principalUnits"],
                    state["ledger"]["chainId"],
                    operation_id=op_id,
                )
                op["evaluation"] = evaluated
            self.save(user, state)
            if not op:
                self.store.notice(user, "MOCK: " + self.decision_message(decision))
                return evaluated
        if before_submit:
            await before_submit()
        async with self.lock(user):
            state = self.read(user)
            self.binding(user, state)
            current = next(o for o in state["operations"] if o["id"] == op["id"])
            if current["state"] != "prepared":
                return {"status": "Prepared move cancelled before broadcast."}
            self.require_approved(state)
            # Fresh data and the active exact policy are checked again at the last submission boundary.
            fresh = await self.rules(
                {
                    "action": "evaluate",
                    "policy": state["plan"]["policy"],
                    "ledger": state["ledger"],
                    "now": str(int(self.clock())),
                    "runId": run_id,
                    "operationId": op_id,
                    "binding": "wallet:" + digest(state["plan"]["policy"]["walletAddress"]),
                    "native": self.native,
                }
            )
            if fresh["result"]["decision"]["action"] != "move":
                current["state"] = "cancelled"
                state["lastResult"] = fresh
                self.save(user, state)
                return fresh
            current["submissionEvaluation"] = fresh
            self.submit_mock(user, state, current)
            return {"operation": self.public_operation(current), **fresh}

    @staticmethod
    def decision_message(d):
        reason = d["reason"] if "reason" in d else d.get("reasons", [])
        calc = d.get("calculation")
        if calc and int(calc["netImprovementMicrounits"]) <= 0:
            return "No move: estimated costs exceed the extra interest, or the destination rate is no better."
        return "No move: " + json.dumps(reason)

    async def pause(self, user):
        async with self.lock(user):
            state = self.read(user)
            self.binding(user, state)
            if not state["plan"]:
                raise ValueError("No plan exists.")
            state["plan"]["status"] = "paused"
            for op in state["operations"]:
                if op["state"] == "prepared":
                    op["state"] = "cancelled"
            self.save(user, state)
            if self.mode == "testnet":
                from agent.lending_live import execute

                await execute(self, user, state, "pause")
            return {"status": "Paused. Prepared operations cancelled. Submitted transfers remain monitored."}

    async def withdraw(self, user):
        async with self.lock(user):
            state = self.read(user)
            wallet = self.binding(user, state)
            if not state["plan"]:
                raise ValueError("No position exists.")
            state["plan"]["status"] = "withdrawing"
            state["withdrawRequested"] = True
            for op in state["operations"]:
                if op["state"] == "prepared":
                    op["state"] = "cancelled"
            self.save(user, state)
            if self.mode == "testnet":
                from agent.lending_live import execute

                return await execute(self, user, state, "withdraw")
            self.continue_withdraw(user, state)
            return {
                "status": "Withdrawal requested. Pending transfers are tracked first; success requires a receipt.",
                "recipient": wallet,
                "networks": NAMES,
            }

    def continue_withdraw(self, user, state):
        if self.pending(state):
            return
        ledger = state["ledger"]
        total = int(ledger["principalUnits"]) + sum(map(int, ledger["idle"].values()))
        if total == 0:
            state["withdrawRequested"] = False
            state["plan"]["status"] = "withdrawn"
            self.save(user, state)
            return
        op = self.operation(state, "withdraw", str(total), ledger["chainId"])
        self.save(user, state)
        self.submit_mock(user, state, op)

    async def tick_user(self, user):
        due = False
        async with self.lock(user):
            state = self.read(user)
            self.binding(user, state)
            if self.mode == "testnet" and state["plan"] and state["plan"]["approvalEvent"]:
                from agent.lending_live import execute

                plan = state["plan"]
                if int(self.clock()) >= int(plan["policy"]["deadline"]) and plan["status"] not in {
                    "withdrawn",
                    "withdrawing",
                }:
                    plan["status"] = "expired"
                    self.save(user, state)
                await execute(self, user, state, "monitor")
                if state["withdrawRequested"]:
                    await execute(self, user, state, "withdraw")
                elif plan["status"] == "awaiting_deposit":
                    await execute(self, user, state, "start")
                elif plan["status"] == "active" and int(self.clock()) >= state["nextCheck"]:
                    await execute(self, user, state, "evaluate")
                return
            if self.mode == "mock":
                self.poll_mock(user, state)
            if self.mode == "testnet":
                observed = await self.testnet_portfolio(user, self.binding(user, state))
                previous = {o["id"]: o["state"] for o in state.get("testnetProgress", {}).get("operations", [])}
                for op in observed["operations"]:
                    if previous.get(op["id"]) != op["state"]:
                        message = {
                            "in_transit": "Still in transit; no destination deposit confirmed.",
                            "verified_deposit": "Destination deposit confirmed by Part 3 receipt verification.",
                            "submitted": "Transfer submitted; not delivered yet.",
                        }.get(op["state"])
                        if message:
                            self.store.notice(user, message)
                state["testnetProgress"] = observed
                self.save(user, state)
            p = state["plan"]
            if not p:
                return
            for op in state["operations"]:
                if op["state"] == "prepared" and int(self.clock()) - op["createdAt"] >= 300:
                    op["state"] = "cancelled"
                    op["cancelReason"] = "Prepared instruction expired without any submission"
                    self.save(user, state)
            if int(self.clock()) >= int(p["policy"]["deadline"]) and p["status"] not in {"withdrawn", "withdrawing"}:
                if p["status"] != "expired":
                    self.store.notice(
                        user,
                        "Plan deadline reached. New investments stopped. "
                        "Existing positions remain deposited until you request withdrawal; "
                        "submitted transfers remain monitored.",
                    )
                p["status"] = "expired"
                for op in state["operations"]:
                    if op["state"] == "prepared":
                        op["state"] = "cancelled"
                self.save(user, state)
            if state["withdrawRequested"] and self.mode == "mock":
                self.continue_withdraw(user, state)
            due = p["status"] == "active" and not self.pending(state) and int(self.clock()) >= state["nextCheck"]
        if due:
            await self.evaluate(user)

    async def tick(self):
        if self.mode == "off" or self.clock() - self.last_scan < 5:
            return
        self.last_scan = self.clock()
        users = self.store.db.execute("SELECT user_id FROM lending_private").fetchall()
        for row in users:
            try:
                await self.tick_user(row[0])
            except (ValueError, OSError) as exc:
                # Do not leak subprocess output, credentials, paths or private provider payloads to chat/logs.
                async with self.lock(row[0]):
                    state = self.read(row[0])
                    code = type(exc).__name__
                    if state["error"] != code:
                        self.store.notice(
                            row[0],
                            "Lending dependency unavailable. New actions stopped; "
                            "existing operations remain recorded. Inspect /lending status.",
                        )
                    state["error"] = code
                    self.save(row[0], state)

    async def status(self, user, operation_id=None):
        async with self.lock(user):
            state = self.read(user)
            wallet = self.binding(user, state)
            if operation_id and self.mode == "testnet":
                observed = await self.testnet_portfolio(user, wallet)
                op = next((o for o in observed["operations"] if o["id"] == operation_id), None)
                if not op:
                    raise ValueError("No such lending operation for this user.")
                return op
            if operation_id:
                op = next((o for o in state["operations"] if o["id"] == operation_id), None)
                if not op:
                    raise ValueError("No such lending operation for this user.")
                return self.public_operation(op)
            p = state["plan"]
            status = p["status"] if p else "no_plan"
            if p and int(self.clock()) >= int(p["policy"]["deadline"]) and status not in {"withdrawn", "withdrawing"}:
                status = "expired"
            result = {
                "mode": self.mode,
                "wallet": wallet,
                "status": status,
                "plan": p,
                "ledger": state["ledger"] if self.mode == "mock" else None,
                "tokensInTransitUnits": self.in_transit(state),
                "earnedProfitUnits": "0",
                "operations": [self.public_operation(o) for o in state["operations"]],
                "lastEvaluation": state["lastResult"],
                "dependencyError": state["error"],
                "deployment": "local CRE execution with an application scheduler"
                if self.native
                else "local shared-rules test harness with an application scheduler; CRE not invoked",
            }
            if self.mode == "testnet":
                result["liveProgress"] = state.get("liveProgress")
                observed = await self.testnet_portfolio(user, wallet)
                state["testnetProgress"] = observed
                self.save(user, state)
                result["testnetProgress"] = observed
                result["tokensInTransitUnits"] = str(
                    sum(
                        int(o["principalUnits"])
                        for o in observed["operations"]
                        if o["state"] in {"source_confirmed", "in_transit", "destination_failed"}
                    )
                )
            return result

    @staticmethod
    def public_operation(op):
        return {k: v for k, v in op.items() if k not in {"evaluation", "submissionEvaluation"}}

    async def markets(self, user):
        portfolio = await self.portfolio(user)
        return {
            "markets": portfolio.get("rates", portfolio.get("chains")),
            "rateUnit": "basis points (100 = 1%)",
            "ratesVariable": True,
            "pricing": "SIM_USD fictional valuations; not live price feeds",
            "interestAccrued": False,
        }

    @staticmethod
    def status_message(status):
        if "kind" in status:
            return json.dumps(status, indent=2)
        lines = [f"{status['mode'].upper()} lending: {status['status']}", f"Wallet: {status['wallet']}"]
        p = status["plan"]
        if p:
            deadline = datetime.fromtimestamp(int(p["policy"]["deadline"]), UTC).isoformat()
            lines.append(f"Plan {p['id']} v{p['version']}; fixed deadline {deadline}")
        ledger = status["ledger"]
        if ledger:
            lines.append(
                f"Deposited principal: {tokens(ledger['principalUnits'])} CCIP-BnM on {NAMES[ledger['chainId']]}"
            )
            for chain in CHAINS:
                lines.append(
                    f"{NAMES[chain]} idle: {tokens(ledger['idle'][chain])} CCIP-BnM; "
                    f"mock fee balance: {tokens(ledger['fees'][chain], 6)} SIM_FEE_ASSET; "
                    f"returned: {tokens(ledger['returned'][chain])} CCIP-BnM"
                )
            lines.append(f"Tokens in transit: {tokens(status['tokensInTransitUnits'])} CCIP-BnM")
        if status.get("testnetProgress"):
            progress = status["testnetProgress"]
            lines.append(f"Tokens in transit: {tokens(status['tokensInTransitUnits'])} CCIP-BnM")
            for chain, data in progress["chains"].items():
                lines.append(
                    f"{NAMES[chain]} finalized block {data['blockNumber']}: "
                    f"principal {tokens(data['principalUnits'])} CCIP-BnM; "
                    f"strategy idle {tokens(data['idleUnits'])} CCIP-BnM; "
                    f"strategy LINK {tokens(data['strategyLinkUnits'])}; "
                    f"owner native ETH {tokens(data['ownerNativeWei'])}."
                )
            for op in progress["operations"]:
                lines.append(
                    f"Part 3 operation {op['id']}: {op['state']}; "
                    f"actual LINK fee units {op['feeToken']['quantityUnits']} "
                    "(null means not yet verified)."
                )
            live = status.get("liveProgress") or {}
            if live.get("status"):
                lines.append("Live execution: " + live["status"])
            for tx in live.get("transactions", [])[-3:]:
                fee = tx["receipt"]["nativeFeeWei"] if tx.get("receipt") else "unconfirmed"
                lines.append(f"{NAMES[tx['chain']]} transaction {tx['hash']}: {tx['state']}; native fee wei {fee}.")
        evaluated = status["lastEvaluation"]
        calc = evaluated["result"]["decision"].get("calculation") if evaluated else None
        if calc:
            lines.append(
                f"Last comparison: projected extra interest "
                f"{tokens(calc['additionalInterestMicrounits'], 6)} SIM_USD; "
                f"incremental costs {tokens(calc['incrementalCostMicrounits'], 6)} SIM_USD; "
                f"projected net improvement {tokens(calc['netImprovementMicrounits'], 6)} SIM_USD."
            )
        lines.append("Earned profit: 0. Mock pools accrue no interest. Projections are not receipts.")
        for op in status["operations"][-3:]:
            lines.append(f"Operation {op['id']}: {op['kind']} — {op['state']}")
        if status.get("dependencyError"):
            lines.append("Dependency unavailable. No new investment was admitted.")
        if status.get("executionBlocker"):
            lines.append(status["executionBlocker"])
        return "\n".join(lines)

    def authenticate(self, user, event_id):
        row = self.store.db.execute(
            "SELECT source,kind,data FROM events WHERE id=? AND user_id=?", (event_id, user)
        ).fetchone()
        data = json.loads(row["data"]) if row else {}
        channel = data.get("channel", {})
        if (
            not row
            or row["source"] != "user"
            or row["kind"] != "chat_message"
            or (channel.get("name") != "whatsapp" or not channel.get("verified_transport"))
        ):
            raise ValueError("Lending requires this user's authenticated WhatsApp session.")

    async def handle(self, user, text, event_id):
        if self.mode == "off":
            return None
        command = text.strip()
        if re.fullmatch(r"/?approve(?:\s+(?:it|this))?[.!]?", command, re.I) and event_id:
            if self.latest_review_command(user, event_id):
                self.authenticate(user, event_id)
                return await self.approve(user, command, event_id)
        if command.startswith(("approve lending ", "/lending")) or command.lower().startswith("manage my test tokens"):
            self.authenticate(user, event_id)
        if command.startswith("approve lending "):
            return await self.approve(user, command, event_id)
        beginner = re.fullmatch(
            r"Manage my test tokens for ([0-9]+) days\.?"
            r"(?: Keep some available and only move the rest when the expected extra interest covers the fees\.?)?",
            command,
            re.I,
        )
        if beginner:
            return (await self.create_plan(user, preset_days=int(beginner[1])))["review"]
        if not command.startswith("/lending"):
            return None
        action = command.removeprefix("/lending").strip()
        if action == "plan":
            return (await self.create_plan(user))["review"]
        if action.startswith("plan "):
            try:
                settings = json.loads(action[5:])
            except ValueError:
                raise ValueError("Use /lending plan followed by a JSON settings object.") from None
            return (await self.create_plan(user, settings))["review"]
        if action == "status":
            return self.status_message(await self.status(user))
        if action in {"markets", "start", "pause", "withdraw"}:
            return json.dumps(await getattr(self, action)(user), indent=2)
        if action.startswith("operation "):
            return json.dumps(await self.status(user, action[10:].strip()), indent=2)
        return "Use /lending plan, start, markets, status, pause, withdraw or operation <id>."


def register_lending_tools(registry):
    def service(ctx):
        lending = ctx.engine.lending
        lending.authenticate(ctx.user_id, ctx.user_event)
        return lending

    @registry.tool(evidence=True, modes=("chat",))
    async def lending_portfolio(ctx):
        """Read this user's verified wallet, principal, idle balances and fee-token quantities.

        Mock data is labelled.
        """
        return await service(ctx).portfolio(ctx.user_id)

    @registry.tool(evidence=True, modes=("chat",))
    async def lending_markets(ctx):
        """Compare the two approved testnet mock lending markets. Rates vary; they are not realized returns."""
        return await service(ctx).markets(ctx.user_id)

    @registry.tool(modes=("chat",))
    async def lending_create_plan(ctx, settings: PlanSettings | None = None):
        """Propose a test plan using explicit expert settings or a labelled beginner preset. Show its exact review.

        Never substitute user identity, wallet, destinations or approval. User must send the exact approval reply.
        """
        values = settings.model_dump() if isinstance(settings, PlanSettings) else settings
        proposed = await service(ctx).create_plan(ctx.user_id, values)
        ctx.review_response = proposed["review"]
        return proposed

    @registry.tool(modes=("chat",))
    async def lending_start(ctx):
        """Start only an already explicitly approved plan. Verify funds and initial deposit before management."""
        return await service(ctx).start(ctx.user_id)

    @registry.tool(modes=("chat",))
    async def lending_pause(ctx):
        """Pause when requested: cancel unsubmitted operations; continue tracking submitted transfers."""
        return await service(ctx).pause(ctx.user_id)

    @registry.tool(modes=("chat",))
    async def lending_withdraw(ctx):
        """On user request, stop investing and return funds on current testnets to this user's verified wallet only."""
        return await service(ctx).withdraw(ctx.user_id)

    @registry.tool(evidence=True, modes=("chat",))
    async def lending_status(ctx, operation_id: str = ""):
        """Inspect this user's plan or operation, pending tokens, separate fee quantities and projected returns."""
        return await service(ctx).status(ctx.user_id, operation_id or None)
