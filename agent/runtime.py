import asyncio
import importlib
import json
import time

from agent.arkiv import ArkivError, Bridge, open_store
from agent.balances import register_balance_tools
from agent.ccip import register_ccip_tools
from agent.ccip_deployment import register_deployment_tools
from agent.ccip_simulation import register_simulation_tools
from agent.chat_commands import ChatCommands
from agent.chat_errors import record_chat_error
from agent.config import Config
from agent.engine import Engine
from agent.lending import Lending, register_lending_tools
from agent.lending_onboarding import LendingOnboarding
from agent.payments import register_payment_tools
from agent.providers import Models
from agent.tools import Registry, register_builtins
from agent.wallet import Wallets, register_wallet_tools


class Runtime:
    def __init__(self, config: Config, models=None):
        self.config = config
        config.workspace.mkdir(parents=True, exist_ok=True)
        self.store = open_store(config)
        self.models = models or Models(config)
        self.registry = Registry()
        register_builtins(self.registry)
        register_wallet_tools(self.registry)
        register_balance_tools(self.registry)
        register_ccip_tools(self.registry)
        register_payment_tools(self.registry)
        register_simulation_tools(self.registry)
        register_deployment_tools(self.registry)
        register_lending_tools(self.registry)
        for module in config.tool_modules:
            importlib.import_module(module).register_tools(self.registry)
        self.engine = Engine(config, self.store, self.models, self.registry)
        self.wallets = Wallets(config, self.store)
        if isinstance(getattr(self.store, "bridge", None), Bridge):
            self.store.bridge.signature_link = lambda user: self.wallets.mobile.issue(user, "arkiv")["metamask_url"]
        self.engine.wallets = self.wallets
        self.lending = Lending(self)
        self.engine.lending = self.lending
        self.lending_onboarding = LendingOnboarding(self.lending)
        self.commands = ChatCommands(self)
        self.jobs = set()
        self.recovered = False
        self.recovery_retry_at = 0
        self.recovery_error = None
        self.last_payment_poll = 0
        self.last_maintenance = 0

    async def chat(self, user_id, text, *, interaction=None):
        try:
            self.recover(strict=False)
            # Commands and model turns share the same per-user lock and history.
            # Payment approval remains a raw incoming-message check before inference.
            return await self.engine.chat(user_id, text, commands=self.commands, interaction=interaction)
        except Exception as exc:
            record_chat_error(self.store, user_id, exc)
            raise

    def recover(self, *, strict=True):
        if not self.recovered and time.monotonic() < self.recovery_retry_at:
            if strict:
                raise ArkivError("Arkiv recovery is waiting for its retry window; task execution is paused.")
            return False
        if not self.recovered:
            try:
                self.store.recover()
            except ArkivError as exc:
                self.recovery_retry_at = time.monotonic() + 60
                self.recovery_error = type(exc).__name__
                if strict:
                    raise
                return False
            self.wallets.restore()
            self.wallets.rabdi.restore()
            self.recovered = True
            self.recovery_error = None
        return True

    async def run_once(self):
        self.recover()
        if self.store.backend == "arkiv":
            self.store.reconcile_checkpoints()
        if task := self.store.claim():
            await self.engine.work(task)
            return True
        return False

    async def worker(self):
        try:
            while True:
                await self.lending_onboarding.tick()
                await self.lending.tick()
                if not self.recover(strict=False):
                    await asyncio.sleep(1)
                    continue
                if self.store.backend == "arkiv":
                    self.store.reconcile_checkpoints()
                if self.store.backend == "arkiv" and time.monotonic() - self.last_maintenance > 3600:
                    self.store.maintain()
                    self.last_maintenance = time.monotonic()
                if time.monotonic() - self.last_payment_poll > 15:
                    self.last_payment_poll = time.monotonic()
                    for row in self.store.db.execute("SELECT user_id,data FROM transfers").fetchall():
                        payment = json.loads(row["data"])
                        if payment["status"] in {
                            "awaiting_signature",
                            "submitting",
                            "submitted",
                            "unknown",
                            "in_transit",
                            "allowance_confirmed",
                        }:
                            try:
                                await self.wallets.status(row["user_id"], payment["id"])
                            except (ValueError, ArkivError):
                                pass  # An RPC failure is not permission to repeat a payment.
                while len(self.jobs) < self.config.max_parallel_tasks and (task := self.store.claim()):
                    job = asyncio.create_task(self.engine.work(task))
                    self.jobs.add(job)
                    job.add_done_callback(self.jobs.discard)
                await asyncio.sleep(0.5)
        finally:
            jobs = list(self.jobs)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def close(self):
        await self.engine.close()
        await self.models.close()
        await self.wallets.close()
        self.store.close()
