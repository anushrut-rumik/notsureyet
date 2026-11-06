"""Testnet wallet tools. Models prepare; a separate human action authorizes spending."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from uuid import uuid4

import httpx
from eth_account import Account
from web3 import Web3

from agent.vault import Vault

SEPOLIA = 11155111
DEFAULT_RPC = "https://ethereum-sepolia-rpc.publicnode.com"


def address(value):
    if not Web3.is_address(value) or int(value, 16) == 0:
        raise ValueError("Use a nonzero Ethereum address")
    if any(c.isupper() for c in value[2:]) and not Web3.is_checksum_address(value):
        raise ValueError("Mixed-case address has an invalid checksum")
    return Web3.to_checksum_address(value)


def amount_wei(value):
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r"\d+(?:\.\d{1,18})?", value):
        raise ValueError("Use an ETH decimal amount with at most 18 decimal places")
    try:
        amount = Decimal(value)
        scaled = amount * 10**18
        if not amount.is_finite() or amount <= 0 or scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError("Amount must be a positive ETH decimal with at most 18 decimal places") from None


class Wallets:
    def __init__(self, config, store, rpc=None):
        self.config, self.store = config, store
        self.vault = Vault(config.data_dir)
        self.client = httpx.AsyncClient(timeout=25)
        self.rpc_override = rpc
        self.lock = asyncio.Lock()
        # Signing custody is independent of the disposable SQLite execution cache.
        for path in config.data_dir.glob("wallet-*.json"):
            wallet = json.loads(path.read_text())
            with store.db:
                store.db.execute(
                    "INSERT OR IGNORE INTO wallets VALUES(?,?,?,?)",
                    (wallet["user_id"], wallet["address"], wallet["kind"], wallet.get("secret")),
                )
        from agent.mobile_wallet import MobileWallet

        self.mobile = MobileWallet(config, self)
        from agent.ccip import CCIP

        self.ccip = CCIP(self)

    def persist_wallet(self, wallet):
        name = hashlib.sha256(wallet["user_id"].encode()).hexdigest()
        target = self.config.data_dir / f"wallet-{name}.json"
        temporary = target.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(wallet, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO wallets VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "address=excluded.address,kind=excluded.kind,secret=excluded.secret",
                (wallet["user_id"], wallet["address"], wallet["kind"], wallet.get("secret")),
            )

    async def close(self):
        self.mobile.close()
        await self.client.aclose()

    async def rpc(self, method, params):
        if self.rpc_override:
            return await self.rpc_override(method, params)
        try:
            response = await self.client.post(
                self.config.env.get("AGENT_SEPOLIA_RPC_URL") or DEFAULT_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise ValueError
            return data["result"]
        except Exception:
            raise ValueError(
                "Sepolia RPC failed; preserve any transaction hash and check again before sending"
            ) from None

    async def network(self):
        if int(await self.rpc("eth_chainId", []), 16) != SEPOLIA:
            raise ValueError("Wrong execution network; only Ethereum Sepolia testnet is enabled")

    def get(self, user):
        name = hashlib.sha256(user.encode()).hexdigest()
        external = self.config.data_dir / f"metamask-{name}.json"
        if external.exists():
            connected = json.loads(external.read_text())
            return {
                "address": address(connected["address"]),
                "kind": "metamask",
                "chain_id": SEPOLIA,
                "network": "Ethereum Sepolia",
                "arkiv": {
                    "address": address(connected["address"]),
                    "chain_id": 7794095,
                    "network": "Arkiv Rabdi",
                    "asset": "tGLM",
                },
            }
        row = self.store.db.execute("SELECT address,kind FROM wallets WHERE user_id=?", (user,)).fetchone()
        if not row:
            raise ValueError("Request an agent wallet or connect a watch-only address in chat first")
        return {
            **dict(row),
            "chain_id": SEPOLIA,
            "network": "Ethereum Sepolia",
            "arkiv": {"address": row["address"], "chain_id": 7794095, "network": "Arkiv Rabdi", "asset": "tGLM"},
        }

    def create(self, user):
        name = hashlib.sha256(user.encode()).hexdigest()
        if (self.config.data_dir / f"metamask-{name}.json").exists():
            raise ValueError("A MetaMask wallet is already connected; creation never replaces it")
        existing = self.store.db.execute("SELECT kind FROM wallets WHERE user_id=?", (user,)).fetchone()
        if existing and existing["kind"] != "watch":
            raise ValueError("A wallet is already configured; creation never replaces an existing wallet")
        account = Account.create()
        secret = self.vault.seal(account.key.hex(), f"wallet:{user}")
        self.persist_wallet({"user_id": user, "address": account.address, "kind": "local", "secret": secret})
        return self.get(user)

    def watch(self, user, wallet_address):
        """An address is read access only. It never grants signing authority."""
        wallet_address = address(wallet_address)
        existing = self.store.db.execute("SELECT kind FROM wallets WHERE user_id=?", (user,)).fetchone()
        if existing and existing["kind"] != "watch":
            raise ValueError("An agent wallet is configured. Read any other address with wallet_balance(address).")
        self.persist_wallet({"user_id": user, "address": wallet_address, "kind": "watch", "secret": None})
        return {**self.get(user), "can_sign": False}

    async def balance(self, user, wallet_address=None):
        owner = address(wallet_address) if wallet_address else self.get(user)["address"]
        await self.network()
        block = await self.rpc("eth_blockNumber", [])
        balance = int(await self.rpc("eth_getBalance", [owner, block]), 16)
        return {
            "address": owner,
            "chain_id": SEPOLIA,
            "asset": "ETH",
            "balance_wei": str(balance),
            "balance": str(Decimal(balance) / 10**18),
            "block": int(block, 16),
        }

    def load(self, user, transfer_id):
        row = self.store.db.execute(
            "SELECT data FROM transfers WHERE id=? AND user_id=?", (transfer_id, user)
        ).fetchone()
        if row is None:
            raise ValueError("No such transfer for this user")
        return json.loads(row[0])

    def save(self, transfer):
        if transfer.get("kind") == "ccip" and transfer.get("phase") == "send" and transfer.get("tx_hash"):
            transfer["source_tx_hash"] = transfer["tx_hash"]
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO transfers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (transfer["id"], transfer["user_id"], transfer["task_id"], json.dumps(transfer)),
            )
        self.store.flush_task(transfer["task_id"])

    @staticmethod
    def view(transfer):
        return {key: value for key, value in transfer.items() if key not in {"raw", "user_id"}}

    def list(self, user):
        return [
            self.view(json.loads(row[0]))
            for row in self.store.db.execute("SELECT data FROM transfers WHERE user_id=?", (user,))
        ]

    async def prepare(self, user, task_id, recipient, amount):
        if self.store.backend != "arkiv":
            raise ValueError("Transfers require AGENT_TASK_BACKEND=arkiv; local-only tasks cannot authorize payments")
        task = self.store.task(user, task_id)
        if task["status"] in {"completed", "cancelled"}:
            raise ValueError("Cannot add a transfer to an ended task")
        wallet = self.get(user)
        if wallet["kind"] not in {"local", "metamask"}:
            raise ValueError("Connect MetaMask through /wallet connect or request a separate agent wallet first.")
        recipient, value = address(recipient), amount_wei(amount)
        maximum = amount_wei(self.config.env.get("AGENT_MAX_TRANSFER_ETH") or "0.01")
        if value > maximum:
            raise ValueError("Transfer exceeds AGENT_MAX_TRANSFER_ETH")
        # One payment intent per task prevents a resumed model from producing a second payment.
        for transfer in self.list(user):
            if transfer["task_id"] == task_id:
                if (
                    transfer.get("kind") == "ccip"
                    or transfer["recipient"] != recipient
                    or int(transfer["value_wei"]) != value
                ):
                    raise ValueError(
                        "This task already has a different payment intent; inspect it before creating new work"
                    )
                return transfer
        await self.network()
        if await self.rpc("eth_getCode", [recipient, "latest"]) != "0x":
            raise ValueError("Direct transfers currently require a recipient without contract code")
        nonce = int(await self.rpc("eth_getTransactionCount", [wallet["address"], "pending"]), 16)
        gas_price = int(await self.rpc("eth_gasPrice", []), 16)
        tx = {"from": wallet["address"], "to": recipient, "value": hex(value), "data": "0x"}
        estimate = int(await self.rpc("eth_estimateGas", [tx, "latest"]), 16)
        gas = (estimate * 12 + 9) // 10
        max_fee = gas * gas_price
        if int(await self.rpc("eth_getBalance", [wallet["address"], "latest"]), 16) < value + max_fee:
            raise ValueError("Insufficient Sepolia ETH for the transfer and its maximum gas fee")
        tx.update(chainId=hex(SEPOLIA), nonce=hex(nonce), gas=hex(gas), gasPrice=hex(gas_price))
        transfer = {
            "id": uuid4().hex,
            "user_id": user,
            "task_id": task_id,
            "status": "awaiting_approval",
            "wallet_kind": wallet["kind"],
            "sender": wallet["address"],
            "recipient": recipient,
            "amount": str(Decimal(value) / 10**18),
            "asset": "ETH",
            "chain_id": SEPOLIA,
            "value_wei": str(value),
            "max_fee_wei": str(max_fee),
            "transaction": tx,
            "created_at": time.time(),
            "expires_at": time.time() + 300,
            "start_block": int(await self.rpc("eth_blockNumber", []), 16),
            "tx_hash": None,
            "approval_code": secrets.token_hex(6).upper(),
        }
        self.save(transfer)
        return self.view(transfer)

    @staticmethod
    def fingerprint(transfer):
        return hashlib.sha256(
            json.dumps(
                {
                    k: transfer[k]
                    for k in ("id", "task_id", "sender", "recipient", "value_wei", "transaction", "approval_code")
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    async def approve(self, user, transfer_id, approval_event_id=None):
        # Only ChatCommands creates this event from raw incoming user text. Never an agent tool.
        async with self.lock:
            transfer = self.load(user, transfer_id)
            if transfer["status"] != "awaiting_approval":
                if transfer["status"] == "awaiting_signature" and not transfer.get("signing_requested_at"):
                    link = self.mobile.issue(user, "payment", transfer_id)
                    return {**self.view(transfer), "confirmation_link": link["metamask_url"]}
                return self.view(transfer)
            approval = self.store.db.execute("SELECT * FROM events WHERE id=?", (approval_event_id,)).fetchone()
            if not approval or (
                approval["user_id"] != user
                or approval["source"] != "user"
                or approval["kind"] != "payment_approval"
                or approval["task_id"] != transfer["task_id"]
                or json.loads(approval["data"]).get("intent") != self.fingerprint(transfer)
            ):
                raise ValueError("Payment requires an explicit incoming approval message for this exact transfer")
            task = self.store.task(user, transfer["task_id"])
            if task["status"] in {"completed", "cancelled"}:
                raise ValueError("The task has ended; approval is disabled")
            refresh_hint = (
                "Refresh the unsigned CCIP review in this same task and approve its new code"
                if transfer.get("kind") == "ccip"
                else "Reject this preview and prepare a new task"
            )
            if time.time() > transfer["expires_at"]:
                raise ValueError("Transfer preview expired. " + refresh_hint)
            wallet = self.get(user)
            if wallet["address"] != transfer["sender"] or wallet["kind"] != transfer["wallet_kind"]:
                raise ValueError("Wallet changed after preparation")
            for other in self.list(user):
                if (
                    other["id"] != transfer_id
                    and other["sender"] == transfer["sender"]
                    and other["status"] in {"awaiting_signature", "submitting", "submitted", "unknown"}
                ):
                    raise ValueError("Reconcile this wallet's earlier transfer before signing another")
            await self.network()
            tx = transfer["transaction"]
            nonce = await self.rpc("eth_getTransactionCount", [transfer["sender"], "pending"])
            if int(nonce, 16) != int(tx["nonce"], 16):
                raise ValueError("Wallet nonce changed. " + refresh_hint)
            if int(await self.rpc("eth_gasPrice", []), 16) > int(tx["gasPrice"], 16):
                raise ValueError("Network fees increased. " + refresh_hint)
            if int(await self.rpc("eth_estimateGas", [tx, "latest"]), 16) > int(tx["gas"], 16):
                raise ValueError("Gas requirement changed. " + refresh_hint)
            if int(await self.rpc("eth_getBalance", [transfer["sender"], "latest"]), 16) < (
                int(transfer["value_wei"]) + int(transfer["max_fee_wei"])
            ):
                raise ValueError("Wallet balance changed; insufficient funds")
            transfer["approved_at"] = time.time()
            if wallet["kind"] == "metamask":
                transfer["status"] = "awaiting_signature"
                self.save(transfer)
                await self.store.checkpoint_barrier(transfer["task_id"])
                link = self.mobile.issue(user, "payment", transfer_id)
                return {**self.view(transfer), "confirmation_link": link["metamask_url"]}
            if wallet["kind"] != "local":
                raise ValueError("Only the separate agent wallet supports chat approval")
            row = self.store.db.execute("SELECT secret FROM wallets WHERE user_id=?", (user,)).fetchone()
            account = Account.from_key(self.vault.open(row[0], f"wallet:{user}"))
            unsigned = {
                k: int(v, 16) if k in {"chainId", "nonce", "gas", "gasPrice", "value"} else v
                for k, v in tx.items()
                if k != "from"
            }
            signed = account.sign_transaction(unsigned)
            transfer.update(
                raw="0x" + signed.raw_transaction.hex(), tx_hash="0x" + signed.hash.hex(), status="submitting"
            )
            self.save(transfer)  # hash and signed bytes are durable before network admission
            await self.store.checkpoint_barrier(transfer["task_id"])
            try:
                result = await self.rpc("eth_sendRawTransaction", [transfer["raw"]])
                if result.lower() != transfer["tx_hash"].lower():
                    raise ValueError("Transaction hash mismatch")
                transfer["status"] = "submitted"
            except Exception:
                transfer["status"] = "unknown"
            self.save(transfer)
            return self.view(transfer)

    def reject(self, user, transfer_id):
        transfer = self.load(user, transfer_id)
        if (
            transfer["status"] not in {"awaiting_approval", "awaiting_signature"}
            or transfer.get("tx_hash")
            or transfer.get("signing_requested_at")
        ):
            raise ValueError("Cannot reject a submitted or uncertain transaction; check its receipt")
        transfer["status"] = "rejected"
        self.save(transfer)
        return self.view(transfer)

    async def status(self, user, transfer_id):
        async with self.lock:
            return await self._status(user, transfer_id)

    async def _status(self, user, transfer_id):
        transfer = self.load(user, transfer_id)
        if transfer.get("kind") == "ccip":
            if transfer["status"] == "in_transit":
                return await self.ccip.delivery(transfer)
            if transfer["status"] == "allowance_confirmed":
                return await self.ccip.advance(transfer)
            if transfer["status"] == "delivered":
                return self.view(transfer)
        if transfer["status"] in {"awaiting_approval", "rejected", "confirmed", "reverted"}:
            return self.view(transfer)
        await self.network()
        if not transfer.get("tx_hash"):
            if transfer["status"] == "awaiting_signature":
                if not transfer.get("signing_requested_at"):
                    return self.view(transfer)
                head = int(await self.rpc("eth_blockNumber", []), 16)
                if head - transfer["start_block"] > 500:
                    raise ValueError("Recover this payment's hash from MetaMask activity; never send it again")
                for number in range(transfer["start_block"], head + 1):
                    block = await self.rpc("eth_getBlockByNumber", [hex(number), True])
                    for tx in block["transactions"]:
                        if self.matches(transfer, tx):
                            transfer.update(tx_hash=tx["hash"], status="submitted")
                            self.save(transfer)
                            break
                    if transfer.get("tx_hash"):
                        break
                if not transfer.get("tx_hash"):
                    return self.view(transfer)
            else:
                raise ValueError("Missing signed transaction reference; stop and inspect the checkpoint")
        tx_hash = transfer["tx_hash"]
        tx = await self.rpc("eth_getTransactionByHash", [tx_hash])
        if tx is None:
            return self.view(transfer)
        if not self.matches(transfer, tx):
            raise ValueError(
                "On-chain transaction does not match the approved payment; no completion or resend allowed"
            )
        receipt = await self.rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return self.view(transfer)
        if (
            receipt.get("transactionHash", "").lower() != tx_hash.lower()
            or tx.get("hash", "").lower() != tx_hash.lower()
        ):
            raise ValueError("RPC returned a different transaction hash; payment is not verified")
        block_number = int(receipt["blockNumber"], 16)
        block = await self.rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if receipt["blockHash"].lower() != block["hash"].lower():
            raise ValueError("Receipt is not in the canonical block; check again")
        head = int(await self.rpc("eth_blockNumber", []), 16)
        confirmations = head - block_number + 1
        if confirmations < 2:
            return {**self.view(transfer), "confirmations": confirmations}
        if transfer.get("kind") == "ccip":
            return await self.ccip.source_receipt(transfer, receipt)
        transfer.update(
            status="confirmed" if int(receipt["status"], 16) == 1 else "reverted",
            block=block_number,
            confirmations=confirmations,
            explorer=f"https://sepolia.etherscan.io/tx/{tx_hash}",
        )
        self.save(transfer)
        # Wake an agent paused for human approval; it can now inspect this receipt.
        task = self.store.task(user, transfer["task_id"])
        if task["status"] == "needs_input":
            self.store.update_task(user, task["id"], status="queued", wake_at=None)
        return self.view(transfer)

    @staticmethod
    def matches(transfer, tx):
        expected = transfer["transaction"]
        return (
            tx["from"].lower() == transfer["sender"].lower()
            and (tx.get("to") or "").lower() == expected["to"].lower()
            and int(tx["value"], 16) == int(transfer["value_wei"])
            and int(tx["nonce"], 16) == int(expected["nonce"], 16)
            and tx.get("input", "0x").lower() == expected.get("data", "0x").lower()
            and int(tx.get("chainId", hex(SEPOLIA)), 16) == SEPOLIA
        )


def register_wallet_tools(registry):
    @registry.tool(evidence=True)
    async def arkiv_status(ctx):
        """Read the active chat wallet's Rabdi tGLM balance and Arkiv storage status. No funds are sent."""
        if ctx.store.backend != "arkiv":
            return {"backend": ctx.store.backend, "enabled": False}
        return ctx.store.bridge.status(ctx.user_id)

    @registry.tool(modes=("chat",))
    async def connect_metamask(ctx):
        """Send a chat-bound MetaMask Mobile setup link. Connecting proves wallet control, not spending authority."""
        return ctx.engine.wallets.mobile.issue(ctx.user_id)

    @registry.tool()
    async def wallet_info(ctx):
        """Read the agent or watched wallet address and network. No spending permission is implied."""
        return ctx.engine.wallets.get(ctx.user_id)

    @registry.tool()
    async def create_wallet(ctx):
        """Create a wallet for payments on Sepolia and task storage on Arkiv Rabdi, using the same address.
        Fund it with Rabdi tGLM for storage and Sepolia ETH for payments. Never replaces an existing wallet.
        """
        return ctx.engine.wallets.create(ctx.user_id)

    @registry.tool()
    async def watch_wallet(ctx, wallet_address: str):
        """Connect a public wallet address for balance reads only. This never grants signing permission."""
        return ctx.engine.wallets.watch(ctx.user_id, wallet_address)

    @registry.tool(evidence=True)
    async def wallet_balance(ctx, wallet_address: str = ""):
        """Read live Sepolia ETH balance at an observed block, for the connected wallet or a public address."""
        return await ctx.engine.wallets.balance(ctx.user_id, wallet_address or None)

    @registry.tool(modes=("task",))
    async def prepare_transfer(ctx, recipient: str, amount_eth: str):
        """Prepare one Sepolia ETH payment for this task. Show its review and ask for approve CODE in chat.
        This does not sign or send. Inspect an existing transfer rather than creating another payment task.
        """
        from agent.chat_commands import ChatCommands

        payment = await ctx.engine.wallets.prepare(ctx.user_id, ctx.task_id, recipient, amount_eth)
        return {**payment, "review_message": ChatCommands.review(payment)}

    @registry.tool()
    async def transfer_status(ctx, transfer_id: str):
        """Reconcile an existing payment against its actual transaction and receipt. Never resubmits it."""
        result = await ctx.engine.wallets.status(ctx.user_id, transfer_id)
        if result["status"] != "confirmed":
            return {**result, "payment_confirmed": False}
        return {**result, "payment_confirmed": True}

    @registry.tool(evidence=True, modes=("task",))
    async def confirmed_transfer_receipt(ctx, transfer_id: str):
        """Read verified payment evidence. Succeeds only after this task's matching transfer has two confirmations."""
        transfer = ctx.engine.wallets.load(ctx.user_id, transfer_id)
        if transfer["task_id"] != ctx.task_id:
            raise ValueError("This transfer belongs to a different task")
        result = await ctx.engine.wallets.status(ctx.user_id, transfer_id)
        if result["status"] != "confirmed":
            raise ValueError("Payment is not confirmed; inspect transfer_status and wait or request approval")
        return {**result, "payment_confirmed": True}
