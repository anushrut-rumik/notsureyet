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
BASE_SEPOLIA = 84532
DEFAULT_RPC = "https://ethereum-sepolia-rpc.publicnode.com"


def address(value):
    if not Web3.is_address(value) or int(value, 16) == 0:
        raise ValueError("Use a nonzero Ethereum address")
    if any(c.isupper() for c in value[2:]) and not Web3.is_checksum_address(value):
        raise ValueError("Mixed-case address has an invalid checksum")
    return Web3.to_checksum_address(value)


def amount_wei(value, *, asset="ETH"):
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r"\d+(?:\.\d{1,18})?", value):
        raise ValueError(f"Use a {asset} decimal amount with at most 18 decimal places")
    try:
        amount = Decimal(value)
        scaled = amount * 10**18
        if not amount.is_finite() or amount <= 0 or scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError(f"Amount must be a positive {asset} decimal with at most 18 decimal places") from None


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
        from agent.rabdi import RabdiTransfers

        self.rabdi = RabdiTransfers(self)

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

    async def base_rpc(self, method, params):
        try:
            response = await self.client.post(
                self.config.env.get("AGENT_BASE_SEPOLIA_RPC_URL") or "https://base-sepolia-rpc.publicnode.com",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise ValueError
            return data["result"]
        except Exception:
            raise ValueError("Base Sepolia RPC failed; preserve the existing hash and do not resend") from None

    def rpc_for(self, chain_id):
        if chain_id == SEPOLIA:
            return self.rpc
        if chain_id == BASE_SEPOLIA:
            return self.base_rpc
        raise ValueError("Only Ethereum Sepolia and Base Sepolia ETH payments are enabled")

    async def network(self, chain_id=SEPOLIA):
        if int(await self.rpc_for(chain_id)("eth_chainId", []), 16) != chain_id:
            raise ValueError("Wrong execution network for the approved testnet payment")

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
                "arkiv": self.storage_wallet(user, address(connected["address"])),
                "agent_wallet": self.agent_wallet(user, required=False),
                "native_transfers": [
                    "ETH on Ethereum Sepolia",
                    "ETH on Base Sepolia (MetaMask)",
                    "tGLM on Arkiv Rabdi",
                ],
            }
        row = self.store.db.execute("SELECT address,kind FROM wallets WHERE user_id=?", (user,)).fetchone()
        if not row:
            raise ValueError("Request an agent wallet or connect a watch-only address in chat first")
        return {
            **dict(row),
            "chain_id": SEPOLIA,
            "network": "Ethereum Sepolia",
            "arkiv": self.storage_wallet(user, row["address"]),
            "agent_wallet": {"address": row["address"], "kind": row["kind"]},
            "native_transfers": ["ETH on Ethereum Sepolia", "ETH on Base Sepolia (MetaMask)", "tGLM on Arkiv Rabdi"],
        }

    def storage_wallet(self, user, fallback):
        bridge = getattr(self.store, "bridge", None)
        wallet = bridge.active(user) if bridge and hasattr(bridge, "active") else {"address": fallback}
        return {"address": wallet["address"], "chain_id": 7794095, "network": "Arkiv Rabdi", "asset": "tGLM"}

    def agent_wallet(self, user, required=True):
        name = hashlib.sha256(user.encode()).hexdigest()
        path = self.config.data_dir / f"wallet-{name}.json"
        if path.exists():
            wallet = json.loads(path.read_text())
            if wallet["user_id"] == user and wallet["kind"] == "local":
                return {"address": address(wallet["address"]), "kind": "local"}
        if required:
            raise ValueError("No agent-created wallet exists in this chat. Specify the intended recipient address.")
        return None

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

    async def balance(self, user, wallet_address=None, *, chain_id=SEPOLIA):
        owner = address(wallet_address) if wallet_address else self.get(user)["address"]
        await self.network(chain_id)
        rpc = self.rpc_for(chain_id)
        block = await rpc("eth_blockNumber", [])
        balance = int(await rpc("eth_getBalance", [owner, block]), 16)
        return {
            "address": owner,
            "chain_id": chain_id,
            "asset": "ETH",
            "balance_wei": str(balance),
            "balance": str(Decimal(balance) / 10**18),
            "block": int(block, 16),
        }

    def journal_path(self, transfer_id):
        if not re.fullmatch(r"[a-f0-9]{32}", transfer_id):
            raise ValueError("Invalid payment reference")
        return self.config.data_dir / f"payment-{transfer_id}.json"

    def persist_transfer(self, transfer):
        # This journal survives SQLite recovery and records one-shot wallet admission before sending.
        from agent.arkiv import Bridge

        task = self.store.task(transfer["user_id"], transfer["task_id"])
        Bridge.save_json(
            self.journal_path(transfer["id"]),
            {
                "encrypted": self.vault.seal(transfer, f"payment:{transfer['id']}"),
                "task": self.vault.seal(task, f"payment-task:{transfer['id']}"),
            },
        )

    def journal_transfer(self, transfer):
        path = self.journal_path(transfer["id"])
        if not path.exists():
            return transfer
        envelope = json.loads(path.read_text())
        saved = self.vault.open(envelope["encrypted"], f"payment:{transfer['id']}")
        if any(saved.get(k) != transfer.get(k) for k in ("id", "user_id", "task_id", "sender")):
            raise ValueError("Payment journal does not match its task and wallet")
        return self.resolve_transfer(transfer, saved)

    @staticmethod
    def resolve_transfer(current, journal):
        """A local admission journal can fill a checkpoint gap, never roll back verified progress."""
        for field in ("id", "user_id", "task_id", "sender", "intent_id"):
            if current.get(field) != journal.get(field):
                raise ValueError("Payment journal identity conflicts with Arkiv")
        for field in ("source_tx_hash", "message_id", "destination_tx_hash"):
            if current.get(field) and journal.get(field) and current[field] != journal[field]:
                raise ValueError("Conflicting protocol references; stop and reconcile")
        if current["status"] in {"confirmed", "delivered", "reverted", "rejected"}:
            return current
        if journal["status"] in {"confirmed", "delivered", "reverted", "rejected"}:
            return journal
        if {current.get("phase"), journal.get("phase")} == {"allowance", "send"}:
            return current if current["phase"] == "send" else journal
        if current.get("tx_hash") and journal.get("tx_hash") and current["tx_hash"] != journal["tx_hash"]:
            raise ValueError("Conflicting payment hashes; stop and reconcile")
        # Never discard a known payment hash or one-shot signing admission for an older snapshot.
        for field in ("source_tx_hash", "message_id", "tx_hash", "signing_requested_at"):
            if current.get(field) and not journal.get(field):
                return current
            if journal.get(field) and not current.get(field):
                return journal
        return max((current, journal), key=lambda p: int(p.get("recorded_at_ns", 0)))

    def restore(self):
        for path in self.config.data_dir.glob("payment-*.json"):
            if not re.fullmatch(r"payment-[a-f0-9]{32}\.json", path.name):
                continue
            transfer_id = path.stem.removeprefix("payment-")
            envelope = json.loads(path.read_text())
            transfer = self.vault.open(envelope["encrypted"], f"payment:{transfer_id}")
            if transfer["id"] != transfer_id:
                raise ValueError("Payment journal identity mismatch")
            task = self.store.db.execute(
                "SELECT user_id,status FROM tasks WHERE id=?", (transfer["task_id"],)
            ).fetchone()
            if (
                not task
                and transfer["status"] == "awaiting_approval"
                and not transfer.get("tx_hash")
                and not transfer.get("signing_requested_at")
                and envelope.get("task")
            ):
                draft = self.vault.open(envelope["task"], f"payment-task:{transfer_id}")
                if draft["id"] != transfer["task_id"] or draft["user_id"] != transfer["user_id"]:
                    raise ValueError("Unsigned payment draft belongs to a different task")
                draft.update(status="needs_input", wake_at=None)
                with self.store.db:
                    self.store.db.execute(
                        "INSERT INTO tasks(" + ",".join(draft) + ") VALUES(" + ",".join("?" for _ in draft) + ")",
                        list(draft.values()),
                    )
                task = self.store.db.execute(
                    "SELECT user_id,status FROM tasks WHERE id=?", (transfer["task_id"],)
                ).fetchone()
            if task and task["user_id"] == transfer["user_id"]:
                cached = self.store.db.execute("SELECT data FROM transfers WHERE id=?", (transfer_id,)).fetchone()
                if cached:
                    transfer = self.resolve_transfer(json.loads(cached[0]), transfer)
                elif task["status"] in {"completed", "cancelled"}:
                    continue  # An obsolete local draft cannot add payments to a terminal checkpoint.
                with self.store.db:
                    self.store.db.execute(
                        "INSERT INTO transfers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (transfer_id, transfer["user_id"], transfer["task_id"], json.dumps(transfer)),
                    )
                    if transfer["status"] in {
                        "awaiting_approval",
                        "awaiting_signature",
                        "submitting",
                        "submitted",
                        "unknown",
                        "in_transit",
                        "allowance_confirmed",
                    }:
                        self.store.db.execute(
                            "UPDATE tasks SET status='needs_input',wake_at=NULL WHERE id=? AND user_id=? "
                            "AND status IN ('queued','running','waiting')",
                            (transfer["task_id"], transfer["user_id"]),
                        )

    def load(self, user, transfer_id):
        row = self.store.db.execute(
            "SELECT data FROM transfers WHERE id=? AND user_id=?", (transfer_id, user)
        ).fetchone()
        if row is None:
            raise ValueError("No such transfer for this user")
        payment = self.journal_transfer(json.loads(row[0]))
        return self.rabdi.load_admission(payment) if payment.get("kind") == "rabdi_native" else payment

    def save(self, transfer, *, checkpoint=True):
        transfer["recorded_at_ns"] = max(time.time_ns(), int(transfer.get("recorded_at_ns", 0)) + 1)
        if transfer.get("kind") == "ccip" and transfer.get("phase") == "send" and transfer.get("tx_hash"):
            transfer["source_tx_hash"] = transfer["tx_hash"]
        self.persist_transfer(transfer)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO transfers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (transfer["id"], transfer["user_id"], transfer["task_id"], json.dumps(transfer)),
            )
        if checkpoint:
            self.store.flush_task(transfer["task_id"])

    @staticmethod
    def view(transfer):
        return {key: value for key, value in transfer.items() if key not in {"raw", "user_id"}}

    def list(self, user):
        return [
            self.view(self.load(user, row[0]))
            for row in self.store.db.execute("SELECT id FROM transfers WHERE user_id=?", (user,))
        ]

    async def prepare(self, user, task_id, recipient, amount, *, chain_id=SEPOLIA, asset="ETH"):
        if self.store.backend != "arkiv":
            raise ValueError("Transfers require AGENT_TASK_BACKEND=arkiv; local-only tasks cannot authorize payments")
        task = self.store.task(user, task_id)
        if task["status"] in {"completed", "cancelled"}:
            raise ValueError("Cannot add a transfer to an ended task")
        rpc = self.rpc_for(chain_id)
        wallet = self.get(user)
        if chain_id == BASE_SEPOLIA and wallet["kind"] != "metamask":
            raise ValueError("Connect MetaMask to fund the agent with Base Sepolia ETH")
        if wallet["kind"] not in {"local", "metamask"}:
            raise ValueError("Connect MetaMask through /wallet connect or request a separate agent wallet first.")
        recipient = address(recipient)
        token = asset == "CCIP-BnM"
        if asset not in {"ETH", "CCIP-BnM"}:
            raise ValueError("Unsupported direct transfer asset")
        if token:
            from agent.token_transfer import preflight

            contract, units, data = await preflight(self, user, chain_id, recipient, amount)
            value = 0
        else:
            value = units = amount_wei(amount)
            maximum = amount_wei(self.config.env.get("AGENT_MAX_TRANSFER_ETH") or "0.01")
            if value > maximum:
                raise ValueError("Transfer exceeds AGENT_MAX_TRANSFER_ETH")
        # One payment intent per task prevents a resumed model from producing a second payment.
        for transfer in self.list(user):
            if transfer["task_id"] == task_id:
                if (
                    transfer.get("asset") != asset
                    or transfer.get("chain_id") != chain_id
                    or transfer["recipient"] != recipient
                    or int(transfer.get("amount_units", transfer["value_wei"])) != units
                    or (token and transfer.get("kind") != "erc20_transfer")
                ):
                    raise ValueError(
                        "This task already has a different payment intent; inspect it before creating new work"
                    )
                return transfer
        await self.network(chain_id)
        if not token and await rpc("eth_getCode", [recipient, "latest"]) != "0x":
            raise ValueError("Direct transfers currently require a recipient without contract code")
        nonce = int(await rpc("eth_getTransactionCount", [wallet["address"], "pending"]), 16)
        gas_price = int(await rpc("eth_gasPrice", []), 16)
        tx = {
            "from": wallet["address"],
            "to": contract if token else recipient,
            "value": hex(value),
            "data": data if token else "0x",
        }
        estimate = int(await rpc("eth_estimateGas", [tx, "latest"]), 16)
        gas = (estimate * 12 + 9) // 10
        max_fee = gas * gas_price
        if int(await rpc("eth_getBalance", [wallet["address"], "latest"]), 16) < value + max_fee:
            raise ValueError("Insufficient testnet ETH for the transfer and its maximum gas fee")
        tx.update(chainId=hex(chain_id), nonce=hex(nonce), gas=hex(gas), gasPrice=hex(gas_price))
        transfer = {
            "id": uuid4().hex,
            "user_id": user,
            "task_id": task_id,
            "status": "awaiting_approval",
            "wallet_kind": wallet["kind"],
            "sender": wallet["address"],
            "recipient": recipient,
            "amount": amount if token else str(Decimal(value) / 10**18),
            "asset": asset,
            "chain_id": chain_id,
            "value_wei": str(value),
            "max_fee_wei": str(max_fee),
            "transaction": tx,
            "created_at": time.time(),
            "expires_at": time.time() + 300,
            "start_block": int(await rpc("eth_blockNumber", []), 16),
            "tx_hash": None,
            "approval_code": secrets.token_hex(6).upper(),
        }
        if token:
            transfer.update(kind="erc20_transfer", token_address=contract, amount_units=str(units), fee_asset="ETH")
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
                    return {
                        **self.view(transfer), "confirmation_link": link["metamask_url"], "review_link": link["url"]
                    }
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
            if transfer.get("kind") == "rabdi_native":
                return await self.rabdi.approve(transfer)
            refresh_hint = (
                "Refresh the unsigned CCIP review in this same task and approve its new code"
                if transfer.get("kind") == "ccip"
                else "Refresh this unsigned payment review in the same task with refresh_payment_review"
            )
            if time.time() > transfer["expires_at"]:
                raise ValueError("Transfer preview expired. " + refresh_hint)
            wallet = self.get(user)
            if wallet["address"] != transfer["sender"] or wallet["kind"] != transfer["wallet_kind"]:
                raise ValueError("Wallet changed after preparation")
            if transfer.get("kind") == "erc20_transfer":
                from agent.token_transfer import preflight

                await preflight(self, user, transfer["chain_id"], transfer["recipient"], transfer["amount"])
            for other in self.list(user):
                if (
                    other["id"] != transfer_id
                    and other["sender"] == transfer["sender"]
                    and other["chain_id"] == transfer["chain_id"]
                    and other["status"] in {"awaiting_signature", "submitting", "submitted", "unknown"}
                ):
                    raise ValueError("Reconcile this wallet's earlier transfer before signing another")
            chain_id = transfer["chain_id"]
            rpc = self.rpc_for(chain_id)
            await self.network(chain_id)
            tx = transfer["transaction"]
            nonce = await rpc("eth_getTransactionCount", [transfer["sender"], "pending"])
            if int(nonce, 16) != int(tx["nonce"], 16):
                raise ValueError("Wallet nonce changed. " + refresh_hint)
            if int(await rpc("eth_gasPrice", []), 16) > int(tx["gasPrice"], 16):
                raise ValueError("Network fees increased. " + refresh_hint)
            if int(await rpc("eth_estimateGas", [tx, "latest"]), 16) > int(tx["gas"], 16):
                raise ValueError("Gas requirement changed. " + refresh_hint)
            if int(await rpc("eth_getBalance", [transfer["sender"], "latest"]), 16) < (
                int(transfer["value_wei"]) + int(transfer["max_fee_wei"])
            ):
                raise ValueError("Wallet balance changed; insufficient funds")
            transfer["approved_at"] = time.time()
            if wallet["kind"] == "metamask":
                transfer["status"] = "awaiting_signature"
                self.save(transfer)
                await self.store.checkpoint_barrier(transfer["task_id"])
                link = self.mobile.issue(user, "payment", transfer_id)
                return {
                    **self.view(transfer), "confirmation_link": link["metamask_url"], "review_link": link["url"]
                }
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
                result = await rpc("eth_sendRawTransaction", [transfer["raw"]])
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
        if transfer.get("kind") == "rabdi_native":
            return await self.rabdi.status(transfer)
        if transfer.get("kind") == "ccip":
            if transfer["status"] == "in_transit":
                return await self.ccip.delivery(transfer)
            if transfer["status"] == "allowance_confirmed":
                return await self.ccip.advance(transfer)
            if transfer["status"] == "delivered":
                return self.view(transfer)
        if transfer["status"] in {"awaiting_approval", "rejected", "confirmed", "reverted"}:
            return self.view(transfer)
        chain_id = transfer["chain_id"]
        rpc = self.rpc_for(chain_id)
        await self.network(chain_id)
        if not transfer.get("tx_hash"):
            if transfer["status"] == "awaiting_signature":
                if not transfer.get("signing_requested_at"):
                    return self.view(transfer)
                head = int(await rpc("eth_blockNumber", []), 16)
                from agent.metamask_receipts import recover

                smart_account = await recover(self, transfer, head)
                if smart_account and not transfer.get("tx_hash"):
                    nonce = int(await rpc("eth_getTransactionCount", [transfer["sender"], hex(head)]), 16)
                    if nonce <= int(transfer["transaction"]["nonce"], 16):
                        return self.view(transfer)
                if not transfer.get("tx_hash") and head - transfer["start_block"] > 500:
                    raise ValueError("Recover this payment's hash from MetaMask activity; never send it again")
                for number in () if transfer.get("tx_hash") else range(transfer["start_block"], head + 1):
                    block = await rpc("eth_getBlockByNumber", [hex(number), True])
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
        tx = await rpc("eth_getTransactionByHash", [tx_hash])
        if tx is None:
            return self.view(transfer)
        if not self.matches(transfer, tx):
            from agent.metamask_receipts import verify

            if not await verify(self, transfer, tx):
                raise ValueError(
                    "On-chain transaction does not match the approved payment; no completion or resend allowed"
                )
            transfer["receipt_kind"] = "metamask_delegation"
        receipt = await rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return self.view(transfer)
        if (
            receipt.get("transactionHash", "").lower() != tx_hash.lower()
            or tx.get("hash", "").lower() != tx_hash.lower()
        ):
            raise ValueError("RPC returned a different transaction hash; payment is not verified")
        block_number = int(receipt["blockNumber"], 16)
        block = await rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if receipt["blockHash"].lower() != block["hash"].lower():
            raise ValueError("Receipt is not in the canonical block; check again")
        head = int(await rpc("eth_blockNumber", []), 16)
        confirmations = head - block_number + 1
        if confirmations < 2:
            return {**self.view(transfer), "confirmations": confirmations}
        if transfer.get("kind") == "ccip_deployment":
            from agent.ccip_deployment import confirmed

            return await confirmed(self, transfer, receipt)
        if transfer.get("kind") == "ccip":
            return await self.ccip.source_receipt(transfer, receipt)
        if transfer.get("kind") == "erc20_transfer" and int(receipt["status"], 16) == 1:
            from agent.token_transfer import receipt_matches

            if not receipt_matches(transfer, receipt):
                raise ValueError("Receipt does not prove the exact token transfer; no completion or resend allowed")
        transfer.update(
            status="confirmed" if int(receipt["status"], 16) == 1 else "reverted",
            block=block_number,
            confirmations=confirmations,
            explorer=("https://sepolia.basescan.org" if chain_id == BASE_SEPOLIA else "https://sepolia.etherscan.io")
            + f"/tx/{tx_hash}",
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
            and (tx.get("to") or "").lower() == (expected.get("to") or "").lower()
            and int(tx["value"], 16) == int(transfer["value_wei"])
            and int(tx["nonce"], 16) == int(expected["nonce"], 16)
            and tx.get("input", "0x").lower() == expected.get("data", "0x").lower()
            and int(tx.get("chainId", hex(transfer["chain_id"])), 16) == transfer["chain_id"]
        )


def register_wallet_tools(registry):
    @registry.tool(evidence=True)
    async def rabdi_balance(ctx, wallet_address: str = ""):
        """Read native tGLM on Arkiv Rabdi for the connected wallet or a specified public address."""
        return await ctx.engine.wallets.rabdi.balance(ctx.user_id, wallet_address)

    @registry.tool(modes=("task",))
    async def prepare_rabdi_transfer(ctx, amount_tglm: str, recipient: str = ""):
        """Prepare a native tGLM transfer on Arkiv Rabdi (7794095). Empty recipient funds this chat's
        agent-created wallet from its active wallet. This does not send funds. Show review_message and
        pause with ask_user. User must approve CODE in chat, then confirm in MetaMask if connected.
        """
        from agent.chat_commands import ChatCommands

        payment = await ctx.engine.wallets.rabdi.prepare(ctx.user_id, ctx.task_id, recipient, amount_tglm)
        return {**payment, "review_message": ChatCommands.review(payment)}

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

    @registry.tool(evidence=True)
    async def base_sepolia_balance(ctx, wallet_address: str = ""):
        """Read Base Sepolia ETH for the connected MetaMask wallet or a public address."""
        return await ctx.engine.wallets.balance(ctx.user_id, wallet_address or None, chain_id=BASE_SEPOLIA)

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
