"""Native Rabdi transfers, with durable admission separate from disposable SQLite."""

import json
import os
import re
import secrets
import time
from decimal import Decimal
from urllib.parse import quote
from uuid import uuid4

from dotenv import dotenv_values
from eth_account import Account

from agent.wallet import address, amount_wei

RABDI = 7794095
EXPLORER = "https://rabdi.explorer.arkiv.network"


def admission_path(config, sender):
    return config.data_dir / f"rabdi-payment-{address(sender).lower()}.json"


def payment_reserved(config, sender):
    path = admission_path(config, sender)
    if not path.exists():
        return False
    return json.loads(path.read_text()).get("state") != "settled"


class RabdiTransfers:
    def __init__(self, wallets):
        self.wallets = wallets
        self.config = wallets.config
        self.rpc_override = None

    async def rpc(self, method, params):
        if self.rpc_override:
            return await self.rpc_override(method, params)
        key = (
            os.environ.get("AGENT_ARKIV_ACCESS_KEY")
            or dotenv_values(self.config.root / ".env").get("AGENT_ARKIV_ACCESS_KEY")
            or self.config.env.get("AGENT_ARKIV_ACCESS_KEY")
            or ""
        )
        # Matches the installed SDK 1.0.1 Rabdi chain definition. Never log this keyed URL.
        url = "https://rpc.rabdi.db-chain.testnet.arkiv.network" + ("/" + quote(key, safe="") if key else "")
        try:
            result = await self.wallets.client.post(
                url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            result.raise_for_status()
            data = result.json()
            if "error" in data:
                raise ValueError
            return data["result"]
        except Exception:
            raise ValueError("Rabdi RPC is unavailable. Preserve any existing hash; do not repeat a payment.") from None

    async def network(self):
        if int(await self.rpc("eth_chainId", []), 16) != RABDI:
            raise ValueError("Wrong payment network; this transfer requires Arkiv Rabdi (7794095)")

    async def balance(self, user, wallet_address=""):
        owner = address(wallet_address) if wallet_address else self.wallets.get(user)["address"]
        await self.network()
        value = int(await self.rpc("eth_getBalance", [owner, "latest"]), 16)
        return {
            "address": owner,
            "chain_id": RABDI,
            "network": "Arkiv Rabdi",
            "asset": "tGLM",
            "balance_wei": str(value),
            "balance": str(Decimal(value) / 10**18),
        }

    async def prepare(self, user, task_id, recipient, amount):
        w = self.wallets
        if w.store.backend != "arkiv":
            raise ValueError("Payments require Arkiv task storage")
        task = w.store.task(user, task_id)
        if task["status"] in {"completed", "cancelled"}:
            raise ValueError("This task has ended")
        wallet = w.get(user)
        if wallet["kind"] not in {"local", "metamask"}:
            raise ValueError("A watch-only address cannot send tGLM; connect MetaMask or create an agent wallet")
        recipient = address(recipient) if recipient else w.agent_wallet(user)["address"]
        value = amount_wei(amount, asset="tGLM")
        if value > amount_wei(self.config.env.get("AGENT_MAX_TRANSFER_TGLM") or "0.01", asset="tGLM"):
            raise ValueError("Transfer exceeds AGENT_MAX_TRANSFER_TGLM")
        if recipient == wallet["address"]:
            raise ValueError("The source and recipient are the same wallet")
        for existing in w.list(user):
            if existing["task_id"] == task_id:
                if (
                    existing.get("kind") != "rabdi_native"
                    or existing["recipient"] != recipient
                    or int(existing["value_wei"]) != value
                ):
                    raise ValueError("This task already has a different payment intent")
                return existing
        await self.network()
        for owner in (wallet["address"], recipient):
            if await self.rpc("eth_getCode", [owner, "latest"]) != "0x":
                raise ValueError("Native tGLM transfers currently support wallets without contract code")
        transfer_id = uuid4().hex
        # A unique public reference lets recovery identify this exact payment even if MetaMask loses its response.
        tx = {
            "from": wallet["address"],
            "to": recipient,
            "value": hex(value),
            "data": "0x" + (b"ira:" + bytes.fromhex(transfer_id)).hex(),
        }
        estimate = int(await self.rpc("eth_estimateGas", [tx, "latest"]), 16)
        gas = max(21000, (estimate * 12 + 9) // 10)
        price = int(await self.rpc("eth_gasPrice", []), 16)
        fee = gas * price
        if fee > amount_wei(self.config.env.get("AGENT_RABDI_MAX_FEE_TGLM") or "0.001", asset="tGLM"):
            raise ValueError("Estimated fee exceeds AGENT_RABDI_MAX_FEE_TGLM")
        if int(await self.rpc("eth_getBalance", [wallet["address"], "latest"]), 16) < value + fee:
            raise ValueError("Insufficient Rabdi tGLM for the amount and maximum network fee")
        # MetaMask selects its nonce at signing time, after the preceding Arkiv transactions.
        tx.update(chainId=hex(RABDI), gas=hex(gas), gasPrice=hex(price))
        transfer = {
            "id": transfer_id,
            "user_id": user,
            "task_id": task_id,
            "kind": "rabdi_native",
            "status": "awaiting_approval",
            "wallet_kind": wallet["kind"],
            "sender": wallet["address"],
            "recipient": recipient,
            "amount": str(Decimal(value) / 10**18),
            "asset": "tGLM",
            "chain_id": RABDI,
            "value_wei": str(value),
            "max_fee_wei": str(fee),
            "transaction": tx,
            "created_at": time.time(),
            "expires_at": time.time() + 300,
            "tx_hash": None,
            "approval_code": secrets.token_hex(6).upper(),
        }
        w.save(transfer)
        return w.view(transfer)

    async def validate(self, payment):
        await self.network()
        wallet = self.wallets.get(payment["user_id"])
        if wallet["address"] != payment["sender"] or wallet["kind"] != payment["wallet_kind"]:
            raise ValueError("The selected wallet changed after this review")
        if time.time() > payment["expires_at"]:
            raise ValueError("The unsigned tGLM review expired; refresh this same intent before approving again")
        tx = payment["transaction"]
        if int(await self.rpc("eth_gasPrice", []), 16) > int(tx["gasPrice"], 16):
            raise ValueError("The Rabdi fee increased; refresh the unsigned review")
        if int(await self.rpc("eth_estimateGas", [tx, "latest"]), 16) > int(tx["gas"], 16):
            raise ValueError("The Rabdi gas requirement increased; refresh the unsigned review")
        if int(await self.rpc("eth_getBalance", [payment["sender"], "latest"]), 16) < (
            int(payment["value_wei"]) + int(payment["max_fee_wei"])
        ):
            raise ValueError("Insufficient Rabdi tGLM for the amount and maximum fee")

    def journal(self, payment, state):
        from agent.arkiv import Bridge

        sender = payment["sender"]
        path = admission_path(self.config, sender)
        if path.exists():
            current = json.loads(path.read_text())
            prior = self.wallets.vault.open(current["encrypted"], f"rabdi-payment:{sender.lower()}")
            if current["state"] != "settled" and prior["id"] != payment["id"]:
                raise ValueError("This wallet already has an admitted Rabdi payment; reconcile it first")
        value = {"state": state, "encrypted": self.wallets.vault.seal(payment, f"rabdi-payment:{sender.lower()}")}
        Bridge.save_json(path, value)

    def reserve(self, payment):
        if payment_reserved(self.config, payment["sender"]):
            raise ValueError("This Rabdi wallet already has an admitted payment. Reconcile its existing hash first.")
        bridge = getattr(self.wallets.store, "bridge", None)
        if bridge and hasattr(bridge, "pending") and bridge.pending(payment["user_id"]):
            raise ValueError("Finish this wallet's pending Arkiv checkpoint before admitting its Rabdi payment")
        self.journal(payment, "admitted")

    @staticmethod
    def validate_admission(original, payment):
        fields = ("id", "user_id", "task_id", "sender", "recipient", "value_wei", "chain_id", "approval_code")
        if any(original.get(k) != payment.get(k) for k in fields):
            raise ValueError("Rabdi admission differs from its recovered task intent")
        fields = ("from", "to", "value", "data", "chainId", "gas", "gasPrice")
        if any(original["transaction"].get(k) != payment["transaction"].get(k) for k in fields):
            raise ValueError("Rabdi admission changed the approved payment details")

    def load_admission(self, original):
        path = admission_path(self.config, original["sender"])
        if not path.exists():
            return original
        envelope = json.loads(path.read_text())
        payment = self.wallets.vault.open(envelope["encrypted"], f"rabdi-payment:{original['sender'].lower()}")
        if payment["id"] != original["id"]:
            return original
        self.validate_admission(original, payment)
        return payment

    async def approve(self, payment):
        w = self.wallets
        if payment_reserved(self.config, payment["sender"]):
            raise ValueError("This Rabdi wallet already has an admitted payment; reconcile it first")
        await self.validate(payment)
        payment["approved_at"] = time.time()
        if payment["wallet_kind"] == "metamask":
            payment["status"] = "awaiting_signature"
            w.save(payment)
            await w.store.checkpoint_barrier(payment["task_id"])
            link = w.mobile.issue(payment["user_id"], "payment", payment["id"])
            return {**w.view(payment), "confirmation_link": link["metamask_url"]}
        w.save(payment)
        await w.store.checkpoint_barrier(payment["task_id"])
        self.reserve(payment)
        try:
            latest = int(await self.rpc("eth_getTransactionCount", [payment["sender"], "latest"]), 16)
            pending = int(await self.rpc("eth_getTransactionCount", [payment["sender"], "pending"]), 16)
            if latest != pending:
                raise ValueError("The Rabdi wallet has a pending transaction; reconcile it before signing")
            await self.validate(payment)
            payment["start_block"] = int(await self.rpc("eth_blockNumber", []), 16)
            payment["transaction"]["nonce"] = hex(latest)
            wallet = w.store.db.execute("SELECT secret FROM wallets WHERE user_id=?", (payment["user_id"],)).fetchone()
            account = Account.from_key(w.vault.open(wallet[0], f"wallet:{payment['user_id']}"))
            tx = {
                k: int(v, 16) if k in {"value", "gas", "gasPrice", "chainId", "nonce"} else v
                for k, v in payment["transaction"].items()
                if k != "from"
            }
            signed = account.sign_transaction(tx)
            payment.update(
                raw="0x" + signed.raw_transaction.hex(), tx_hash="0x" + signed.hash.hex(), status="submitting"
            )
            self.journal(payment, "submitting")  # Durable signed bytes and hash before the only send call.
            w.save(payment)
        except Exception:
            # No wallet/network send was reached. Retain the failed attempt and require a fresh review.
            if not payment.get("tx_hash"):
                self.journal(payment, "settled")
            raise
        try:
            result = await self.rpc("eth_sendRawTransaction", [payment["raw"]])
            if result.lower() != payment["tx_hash"].lower():
                raise ValueError("Rabdi returned a different transaction hash")
            payment["status"] = "submitted"
        except Exception:
            payment["status"] = "unknown"
        self.journal(payment, "submitted")
        w.save(payment)
        return w.view(payment)

    async def admit_metamask(self, payment):
        await self.validate(payment)
        payment["start_block"] = int(await self.rpc("eth_blockNumber", []), 16)
        # The one-shot signature marker is already on Arkiv. Reserve storage writes until receipt verification.
        self.reserve(payment)
        self.wallets.save(payment)
        return {"transaction": payment["transaction"], "transfer_id": payment["id"]}

    @staticmethod
    def matches(payment, tx):
        expected = payment["transaction"]
        try:
            return (
                tx["from"].lower() == payment["sender"].lower()
                and (tx.get("to") or "").lower() == payment["recipient"].lower()
                and int(tx["value"], 16) == int(payment["value_wei"])
                and tx.get("input", tx.get("data", "0x")).lower() == expected["data"].lower()
                and int(tx.get("chainId", "0x0"), 16) == RABDI
                and int(tx["gas"], 16) * int(tx.get("maxFeePerGas", tx.get("gasPrice", "0x0")), 16)
                <= int(payment["max_fee_wei"])
                and ("nonce" not in expected or int(tx["nonce"], 16) == int(expected["nonce"], 16))
            )
        except (KeyError, TypeError, ValueError):
            return False

    async def attach(self, payment, tx_hash):
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
            raise ValueError("Use the existing transaction hash from MetaMask")
        if payment.get("tx_hash"):
            if payment["tx_hash"].lower() != tx_hash.lower():
                raise ValueError("This payment already has a different transaction hash")
            return {"saved": True, "tx_hash": payment["tx_hash"]}
        await self.network()
        tx = await self.rpc("eth_getTransactionByHash", [tx_hash])
        if not tx or tx.get("hash", "").lower() != tx_hash.lower() or not self.matches(payment, tx):
            raise ValueError("This hash does not yet identify the approved Rabdi payment; do not send again")
        payment.update(tx_hash=tx_hash, status="submitted")
        self.journal(payment, "submitted")
        self.wallets.save(payment)
        return {"saved": True, "tx_hash": tx_hash}

    async def status(self, payment):
        w = self.wallets
        if payment["status"] in {"awaiting_approval", "rejected", "confirmed", "reverted"}:
            return w.view(payment)
        if not payment.get("signing_requested_at") and payment["status"] == "awaiting_signature":
            return w.view(payment)
        await self.network()
        if not payment.get("tx_hash"):
            head = int(await self.rpc("eth_blockNumber", []), 16)
            start = int(payment.get("scan_next", payment["start_block"]))
            if head - payment["start_block"] > 500:
                raise ValueError("Save this Rabdi payment's existing hash from MetaMask; never send it again")
            for number in range(start, min(head + 1, start + 20)):
                block = await self.rpc("eth_getBlockByNumber", [hex(number), True])
                for tx in block["transactions"]:
                    if self.matches(payment, tx):
                        payment.update(tx_hash=tx["hash"], status="submitted")
                        break
                payment["scan_next"] = number + 1
                if payment.get("tx_hash"):
                    break
            self.journal(payment, "submitted" if payment.get("tx_hash") else "admitted")
            w.save(payment)
            if not payment.get("tx_hash"):
                return w.view(payment)
        tx_hash = payment["tx_hash"]
        tx = await self.rpc("eth_getTransactionByHash", [tx_hash])
        if not tx:
            return w.view(payment)
        if tx.get("hash", "").lower() != tx_hash.lower() or not self.matches(payment, tx):
            raise ValueError("Rabdi transaction differs from the approved payment; no resend allowed")
        receipt = await self.rpc("eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            return w.view(payment)
        if receipt.get("transactionHash", "").lower() != tx_hash.lower():
            raise ValueError("Rabdi returned the wrong receipt")
        block = await self.rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if block["hash"].lower() != receipt["blockHash"].lower():
            raise ValueError("Rabdi receipt is not canonical; preserve the existing hash")
        confirmations = int(await self.rpc("eth_blockNumber", []), 16) - int(receipt["blockNumber"], 16) + 1
        if confirmations < 2:
            return {**w.view(payment), "confirmations": confirmations}
        payment.update(
            status="confirmed" if int(receipt["status"], 16) == 1 else "reverted",
            confirmations=confirmations,
            block=int(receipt["blockNumber"], 16),
            explorer=f"{EXPLORER}/tx/{tx_hash}",
        )
        self.journal(payment, "settled")
        w.save(payment)
        if (
            payment["status"] == "confirmed"
            and w.store.task(payment["user_id"], payment["task_id"])["status"] == "needs_input"
        ):
            w.store.update_task(payment["user_id"], payment["task_id"], status="queued", wake_at=None)
        return w.view(payment)

    def restore(self):
        """Recover admitted hashes after Arkiv restores the task cache. Never signs or sends."""
        w = self.wallets
        for path in self.config.data_dir.glob("rabdi-payment-*.json"):
            sender = path.stem.removeprefix("rabdi-payment-")
            envelope = json.loads(path.read_text())
            payment = w.vault.open(envelope["encrypted"], f"rabdi-payment:{sender}")
            if payment["sender"].lower() != sender or payment["kind"] != "rabdi_native":
                raise ValueError("Invalid Rabdi admission journal")
            try:
                task = w.store.task(payment["user_id"], payment["task_id"])
                row = w.store.db.execute(
                    "SELECT data FROM transfers WHERE id=? AND user_id=?", (payment["id"], payment["user_id"])
                ).fetchone()
                if row is None:
                    continue
                original = json.loads(row[0])
            except ValueError:
                continue  # No remote authority for this task; retain the journal without executing it.
            self.validate_admission(original, payment)
            with w.store.db:
                w.store.db.execute(
                    "UPDATE transfers SET data=? WHERE id=? AND user_id=?",
                    (json.dumps(payment), payment["id"], payment["user_id"]),
                )
                if payment["status"] == "confirmed" and task["status"] == "needs_input":
                    w.store.db.execute("UPDATE tasks SET status='queued',wake_at=NULL WHERE id=?", (task["id"],))
