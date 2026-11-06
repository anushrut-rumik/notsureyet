"""Short-lived, chat-bound MetaMask Mobile links. Never holds a MetaMask signing key."""

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.messages import encode_defunct

from agent.wallet import SEPOLIA, address


class MobileWallet:
    def __init__(self, config, wallets):
        self.config, self.wallets = config, wallets
        self.origin = (config.env.get("AGENT_WALLET_PUBLIC_URL") or "").rstrip("/")
        if self.origin:
            parsed = urlsplit(self.origin)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("AGENT_WALLET_PUBLIC_URL must be an HTTPS origin")
        path = config.data_dir / "wallet-links.sqlite3"
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS links (digest TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.lock = asyncio.Lock()

    def close(self):
        self.db.close()

    def _save(self, digest, data):
        with self.db:
            self.db.execute(
                "INSERT INTO links VALUES(?,?) ON CONFLICT(digest) DO UPDATE SET data=excluded.data",
                (digest, json.dumps(data)),
            )

    def _load(self, token, *, result=False):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.execute("SELECT data FROM links WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ValueError("This wallet link is invalid. Request a new link in WhatsApp.")
        data = json.loads(row[0])
        deadline = data["created_at"] + 86400 if result and data.get("claimed") else data["expires_at"]
        if time.time() > deadline:
            raise ValueError("This wallet link expired. Request a new link in WhatsApp.")
        return digest, data

    def issue(self, user, kind="connect", transfer_id=None):
        if not self.origin:
            raise ValueError("MetaMask mobile links are not configured on this agent yet")
        token = secrets.token_urlsafe(32)
        data = {
            "user_id": user,
            "kind": kind,
            "transfer_id": transfer_id,
            "created_at": time.time(),
            "expires_at": time.time() + (600 if kind == "connect" else 300),
            "claimed": False,
        }
        if kind == "payment":
            payment = self.wallets.load(user, transfer_id)
            data["fingerprint"] = self.wallets.fingerprint(payment)
        self._save(hashlib.sha256(token.encode()).hexdigest(), data)
        if kind not in {"connect", "payment", "arkiv"}:
            raise ValueError("Unknown wallet request")
        if kind == "arkiv":
            pending = self.wallets.store.bridge.pending(user)
            if not pending:
                raise ValueError("No Arkiv checkpoint is waiting for approval")
            data.update(operation=pending["operation"], instance=pending["instance"])
            self._save(hashlib.sha256(token.encode()).hexdigest(), data)
        url = f"{self.origin}/{kind}/{token}"
        return {
            "url": url,
            "metamask_url": "https://link.metamask.io/dapp/" + url.removeprefix("https://"),
            "expires_at": data["expires_at"],
        }

    def check_intent(self, data, payment):
        if data.get("fingerprint") and data["fingerprint"] != self.wallets.fingerprint(payment):
            raise ValueError("This link belongs to an earlier payment step. Use the current review in WhatsApp.")

    def info(self, token):
        _, data = self._load(token, result=True)
        if data["kind"] == "arkiv":
            bridge = self.wallets.store.bridge
            scope = bridge.scope(data["user_id"])
            journal = bridge.journal(scope)
            if (
                not journal
                or journal.get("instance") != data["instance"]
                or journal.get("operation") != data["operation"]
            ):
                raise ValueError("No Arkiv checkpoint is available for this wallet")
            return {
                "kind": "arkiv",
                "claimed": journal["state"] != "external_prepared",
                "payment": {
                    "sender": scope["writer"],
                    "recipient": journal["transaction"]["to"],
                    "amount": "0",
                    "asset": "tGLM",
                    "chain_id": 7794095,
                    "status": journal["state"],
                    "max_fee_wei": str(
                        int(journal["transaction"]["gas"], 16) * int(journal["transaction"]["gasPrice"], 16)
                    ),
                    "tx_hash": journal.get("hash"),
                    "task_id": journal["task"],
                },
            }
        if data["kind"] == "connect":
            return {
                "kind": "connect",
                "completed": bool(data.get("completed")),
                "origin": self.origin,
            }
        payment = self.wallets.load(data["user_id"], data["transfer_id"])
        self.check_intent(data, payment)
        return {
            "kind": "payment",
            "claimed": data["claimed"],
            "payment": {
                **{
                    k: payment[k]
                    for k in ("kind", "phase", "destination", "sender_contract", "ccip_fee_eth", "ccip_fee_cap_eth")
                    if k in payment
                },
                **{
                    k: payment[k]
                    for k in (
                        "id",
                        "sender",
                        "recipient",
                        "amount",
                        "asset",
                        "chain_id",
                        "max_fee_wei",
                        "status",
                        "tx_hash",
                    )
                },
            },
        }

    def progress(self, token, stage, error_code=0):
        digest, data = self._load(token, result=True)
        allowed = {
            "page_loaded",
            "connect_clicked",
            "network_requested",
            "permissions_requested",
            "permissions_returned",
            "accounts_requested",
            "accounts_returned",
            "challenge_received",
            "signature_requested",
            "signature_returned",
            "completed",
            "wallet_error",
        }
        if stage not in allowed:
            raise ValueError("Unknown wallet connection step")
        data["progress"] = (data.get("progress", []) + [{"stage": stage, "error_code": error_code, "at": time.time()}])[
            -20:
        ]
        self._save(digest, data)
        return {"recorded": True}

    def challenge(self, token, wallet_address, chain_id=SEPOLIA):
        digest, data = self._load(token)
        if data["kind"] != "connect" or data.get("completed"):
            raise ValueError("This connection link has already been used")
        account = address(wallet_address)
        if type(chain_id) is not int or chain_id <= 0 or chain_id > 2**53 - 1:
            raise ValueError("MetaMask returned an invalid network identifier")
        nonce = secrets.token_hex(16)
        issued = datetime.now(UTC).isoformat()
        expiration = datetime.fromtimestamp(data["expires_at"], UTC).isoformat()
        message = (
            f"{urlsplit(self.origin).netloc} wants you to sign in with your Ethereum account:\n{account}\n\n"
            "Connect this wallet to your Ira WhatsApp chat. This does not authorize a payment or spending.\n\n"
            f"URI: {self.origin}\nVersion: 1\nChain ID: {chain_id}\nNonce: {nonce}\n"
            f"Issued At: {issued}\nExpiration Time: {expiration}"
        )
        data.update(address=account, message=message)
        self._save(digest, data)
        return {"message": message, "address": account}

    def connect(self, token, signature):
        digest, data = self._load(token)
        if data["kind"] != "connect" or data.get("completed") or not data.get("message"):
            raise ValueError("Request a fresh wallet connection challenge")
        try:
            account = Account.recover_message(encode_defunct(text=data["message"]), signature=signature)
        except Exception:
            raise ValueError("MetaMask connection signature is invalid") from None
        if account.lower() != data["address"].lower():
            raise ValueError("The signature does not match the selected wallet")
        # Keep the original agent-wallet key file intact. External connection is a separate preference.
        name = hashlib.sha256(data["user_id"].encode()).hexdigest()
        path = self.config.data_dir / f"metamask-{name}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({"user_id": data["user_id"], "address": account, "kind": "metamask"}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        data.update(completed=True)
        self._save(digest, data)
        self.wallets.store.notice(
            data["user_id"],
            f"MetaMask connected: {account}. Your payment requests and "
            "receipts stay in this chat. This same address is used for Arkiv storage on Rabdi; "
            "fund it with Rabdi tGLM. Each storage checkpoint and transfer needs your confirmation in MetaMask.",
        )
        return {"address": account, "connected": True}

    async def claim(self, token):
        _, data = self._load(token)
        if data["kind"] == "arkiv":
            # A payment may hold the wallet lock while waiting for this storage signature.
            return await self._claim(token)
        async with self.wallets.lock:
            return await self._claim(token)

    async def _claim(self, token):
        async with self.lock:
            digest, data = self._load(token)
            if data["kind"] == "arkiv":
                if data["claimed"]:
                    raise ValueError("This Arkiv signing request already started. Check the existing transaction.")
                self.info(token)  # Bind this capability to its original wallet and checkpoint operation.
                result = self.wallets.store.bridge.claim_external(data["user_id"])
                data["claimed"] = True
                self._save(digest, data)
                return result
            if data["kind"] != "payment" or data["claimed"]:
                raise ValueError("A MetaMask request already started. Check wallet activity; do not send again.")
            payment = self.wallets.load(data["user_id"], data["transfer_id"])
            self.check_intent(data, payment)
            if payment["wallet_kind"] != "metamask" or payment["status"] != "awaiting_signature":
                raise ValueError("This payment is not waiting for a MetaMask signature")
            if payment.get("signing_requested_at"):
                raise ValueError("A MetaMask request already started for this payment. Check its existing activity.")
            refresh_hint = (
                "Ask Ira to refresh this unsigned CCIP review in the same task, then approve the new code."
                if payment.get("kind") == "ccip"
                else "Prepare a fresh payment in chat."
            )
            if time.time() > payment["expires_at"]:
                raise ValueError("The payment preview expired. " + refresh_hint)
            task = self.wallets.store.task(data["user_id"], payment["task_id"])
            if task["status"] in {"completed", "cancelled"}:
                raise ValueError("This payment task has ended")
            native_rabdi = payment.get("kind") == "rabdi_native"
            if native_rabdi:
                await self.wallets.rabdi.validate(payment)
            else:
                await self.wallets.network()
                nonce = int(await self.wallets.rpc("eth_getTransactionCount", [payment["sender"], "pending"]), 16)
                if nonce != int(payment["transaction"]["nonce"], 16):
                    raise ValueError("The wallet nonce changed. " + refresh_hint)
            transaction = payment["transaction"]
            rpc = self.wallets.rabdi.rpc if native_rabdi else self.wallets.rpc
            if int(await rpc("eth_gasPrice", []), 16) > int(transaction["gasPrice"], 16):
                raise ValueError("The network fee changed. " + refresh_hint)
            if int(await rpc("eth_estimateGas", [transaction, "latest"]), 16) > int(transaction["gas"], 16):
                raise ValueError("The gas requirement changed. " + refresh_hint)
            balance = int(await rpc("eth_getBalance", [payment["sender"], "latest"]), 16)
            if balance < int(payment["value_wei"]) + int(payment["max_fee_wei"]):
                raise ValueError(f"Insufficient {payment['asset']} for this payment and its maximum gas fee")
            data["claimed"] = True
            self._save(digest, data)
            # The claim is one-shot even if a later checkpoint fails. It cannot reopen a signing request.
            payment["signing_requested_at"] = time.time()
            payment["start_block"] = int(await rpc("eth_blockNumber", []), 16)
            self.wallets.save(payment, checkpoint=native_rabdi)
        if payment.get("kind") == "rabdi_native":
            await self.wallets.store.checkpoint_barrier(payment["task_id"])
            return await self.wallets.rabdi.admit_metamask(payment)
        return {"transaction": payment["transaction"], "transfer_id": payment["id"]}

    async def attach(self, token, tx_hash):
        _, data = self._load(token, result=True)
        if data["kind"] == "arkiv":
            return await self._attach(token, tx_hash)
        async with self.wallets.lock:
            return await self._attach(token, tx_hash)

    async def _attach(self, token, tx_hash):
        _, data = self._load(token, result=True)
        if data["kind"] == "arkiv":
            self.info(token)
            if not data["claimed"]:
                raise ValueError("No Arkiv signing request was admitted through this link")
            self.wallets.store.bridge.attach_external(data["user_id"], tx_hash)
            self.wallets.store.accept_checkpoint(data["user_id"])
            self.wallets.store.notice(data["user_id"], f"Task checkpoint stored on Arkiv Rabdi: {tx_hash}")
            return {"saved": True, "tx_hash": tx_hash}
        if data["kind"] != "payment" or not data["claimed"]:
            raise ValueError("This payment link has no admitted MetaMask request")
        if not isinstance(tx_hash, str) or len(tx_hash) != 66 or not tx_hash.startswith("0x"):
            raise ValueError("Use the transaction hash returned by MetaMask")
        try:
            bytes.fromhex(tx_hash[2:])
        except ValueError:
            raise ValueError("Invalid transaction hash") from None
        payment = self.wallets.load(data["user_id"], data["transfer_id"])
        self.check_intent(data, payment)
        if payment.get("kind") == "rabdi_native":
            return await self.wallets.rabdi.attach(payment, tx_hash)
        if payment.get("tx_hash"):
            if payment["tx_hash"].lower() != tx_hash.lower():
                raise ValueError("This payment already has a different transaction reference")
            return {"saved": True, "tx_hash": payment["tx_hash"]}
        await self.wallets.network()
        tx = await self.wallets.rpc("eth_getTransactionByHash", [tx_hash])
        if tx is None:
            raise ValueError("The network has not indexed this hash yet. Save the same hash again; do not send again.")
        if tx.get("hash", "").lower() != tx_hash.lower() or not self.wallets.matches(payment, tx):
            raise ValueError("This transaction does not match the approved payment")
        payment.update(tx_hash=tx_hash, status="submitted")
        self.wallets.save(payment, checkpoint=False)
        self.wallets.store.notice(
            data["user_id"],
            f"MetaMask payment submitted: {tx_hash}. I will check its receipt and keep this task saved.",
        )
        return {"saved": True, "tx_hash": tx_hash}
