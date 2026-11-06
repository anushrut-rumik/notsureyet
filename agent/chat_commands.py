"""Raw user-message handling. The model has no route to authorize a payment."""

import json
import re
import time
from decimal import Decimal

from agent.wallet import Wallets


class ChatCommands:
    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def store(self):
        return self.runtime.store

    @property
    def wallets(self):
        return self.runtime.wallets

    def recent_approval_code(self, user):
        """Resolve a short reply against the latest approval review shown in this chat."""
        rows = self.store.db.execute(
            "SELECT data FROM events WHERE user_id=? AND kind='chat_message' AND source!='user' ORDER BY id DESC",
            (user,),
        )
        for row in rows:
            message = json.loads(row["data"])
            if message.get("role") != "assistant":
                continue
            text = message.get("content", "")
            # A different approval flow must not fall back to an older payment.
            if "approve lending " in text.lower():
                return None
            codes = set(re.findall(r"\bapprove\s+([0-9a-f]{12})\b", text, re.I))
            if codes:
                return next(iter(codes)).upper() if len(codes) == 1 else None
        return None

    @staticmethod
    def review(payment):
        if payment.get("kind") == "ccip_deployment":
            from agent.ccip_deployment import review

            return review(payment)
        if payment.get("kind") == "ccip":
            phase = "Approve token allowance" if payment["phase"] == "allowance" else "Send cross-chain transfer"
            return (
                f"Review CCIP transfer {payment['id']} · {phase}\n"
                f"Amount: {payment['amount']} CCIP-BnM\n"
                f"From: {payment['sender']} on Ethereum Sepolia\n"
                f"Recipient: {payment['recipient']}\nDestination: Base Sepolia (84532)\n"
                f"Sender contract / allowance spender: {payment['sender_contract']}\n"
                f"CCIP fee quote: {payment['ccip_fee_eth']} ETH\n"
                f"Maximum CCIP fee: {payment['ccip_fee_cap_eth']} ETH (unused portion refunded)\n"
                f"Maximum gas for this step: {Decimal(payment['max_fee_wei']) / 10**18} ETH\n"
                "Allowance approval only enables the exact token amount; it does not send across chains. "
                "If needed, the send has a separate review and gas fee. Delivery remains pending until verified.\n"
                "This review expires in 5 minutes.\n"
                f"Reply approve to continue, or reject to cancel.\nPayment code: approve {payment['approval_code']}"
            )
        network = {7794095: "Arkiv Rabdi (7794095)", 84532: "Base Sepolia (84532)", 11155111: "Ethereum Sepolia"}[
            payment["chain_id"]
        ]
        return (
            f"Review payment {payment['id']}\n"
            f"Send: {payment['amount']} {payment['asset']} on {network}\n"
            f"From: {payment['sender']}\nTo: {payment['recipient']}\n"
            f"Execution gas budget: {Decimal(payment['max_fee_wei']) / 10**18} "
            f"{payment.get('fee_asset', payment['asset'])}\n"
            + (
                f"Token contract: {payment['token_address']}\nSame-chain token transfer; no bridge.\n"
                if payment.get("kind") == "erc20_transfer"
                else ""
            )
            + (
                "Base adds an L1 data fee; review the full network fee in MetaMask.\n"
                if payment["chain_id"] == 84532
                else ""
            )
            + "Expires in 5 minutes. No funds have been sent.\n"
            "Reply approve to continue, or reject to cancel.\n"
            f"Payment code: approve {payment['approval_code']}"
        )

    async def handle(self, user, text, *, user_event=None):
        lending_reply = await self.runtime.lending.handle(user, text, user_event)
        if lending_reply is not None:
            return lending_reply
        command = text.strip()
        if command.lower().lstrip("/") in {"wallet connect", "wallet metamask", "connect metamask"}:
            link = self.wallets.mobile.issue(user)
            return (
                "Connect your MetaMask wallet using this link (valid for 10 minutes):\n"
                f"{link['metamask_url']}\n"
                "You will sign a connection message. Each payment still needs a MetaMask confirmation."
            )
        if command.lower().lstrip("/") == "wallet create":
            wallet = self.wallets.create(user)
            return (
                f"Your agent wallet: {wallet['address']}\n"
                "Fund this same address with tGLM on Arkiv Rabdi for task storage, "
                "and test ETH on Ethereum Sepolia for payments. These are separate network balances. "
                "Arkiv checkpoints use this wallet automatically within the configured fee limit. "
                "Payments require your explicit approval message in this chat. "
                "Its signing key stays encrypted on the server."
            )
        match = re.fullmatch(r"/?wallet (?:connect|watch) (0x[0-9a-fA-F]{40})", command, re.I)
        if match:
            wallet = self.wallets.watch(user, match[1])
            return (
                f"Watching {wallet['address']} on Sepolia for balance reads. "
                "This does not grant spending access. Use /wallet create for a separate agent wallet."
            )
        match = re.fullmatch(r"/?balance(?: (0x[0-9a-fA-F]{40}))?", command, re.I)
        if match:
            balance = await self.wallets.balance(user, match[1])
            return f"{balance['address']}: {balance['balance']} Sepolia ETH (block {balance['block']})."
        if command.lower().lstrip("/") == "tasks":
            tasks = self.store.tasks(user)
            if not tasks:
                return f"No tasks found in {self.store.backend}."
            return "\n".join(f"{task['id']} · {task['status']} · {task['title']}" for task in tasks)
        match = re.fullmatch(r"/?task ([0-9a-f]{32})", command, re.I)
        if match:
            progress = self.store.progress(user, match[1])
            task = progress["task"]
            state = progress.get("public_state", {})
            refs = "\n".join(
                f"{key}: {state[key]}"
                for key in ("protocol", "progress", "reference", "source_tx", "order_id", "destination_tx")
                if state.get(key)
            )
            return f"{task['title']}: {task['status']}\n{task['notes']}" + ("\n" + refs if refs else "")
        if command.lower().lstrip("/") == "recover":
            if self.runtime.jobs:
                return "A task is still running. Wait for it to pause before requesting recovery."
            self.runtime.recovered = False
            self.runtime.recovery_retry_at = 0
            self.runtime.recover()
            return f"Recovered task checkpoints from {self.store.backend}. No payment was resubmitted."
        match = re.fullmatch(r"/?arkiv wallet (agent|connected|active)", command, re.I)
        if match:
            if self.store.backend != "arkiv":
                return "Enable Arkiv task storage before choosing its wallet."
            scope = self.store.bridge.select_wallet(user, match[1].lower())
            return (
                f"New Arkiv tasks will use {scope['writer']} on Rabdi. "
                "Existing tasks retain their original wallet. Agent-created wallets sign checkpoints automatically."
            )
        if command.lower().lstrip("/") == "arkiv":
            if self.store.backend != "arkiv":
                return "Task storage is local SQLite. Enable Arkiv before making payments."
            status = self.store.bridge.status(user)
            balance = Decimal(status["balance_wei"]) / 10**18
            mode = (
                "Each checkpoint needs your confirmation in MetaMask."
                if status["kind"] == "metamask"
                else "Checkpoints are signed by your agent wallet automatically."
            )
            pending = (
                "\nApprove the pending checkpoint: " + self.wallets.mobile.issue(user, "arkiv")["metamask_url"]
                if status["pending"]
                else ""
            )
            return (
                f"Your Arkiv wallet: {status['writer']}\nNetwork: Rabdi (7794095)\n"
                f"Balance: {balance} tGLM\nPublic query scope: {status['instance']}\n"
                "Send Rabdi tGLM to this wallet for task storage. Active records last 30 days; "
                "completed/cancelled records last 180 days. " + mode + pending
            )
        match = re.fullmatch(r"/?transfer ([0-9.]+) (0x[0-9a-fA-F]{40})", command, re.I)
        if match:
            from agent.payments import prepare_request

            payment = await prepare_request(
                self.wallets,
                user,
                asset="ETH",
                amount=match[1],
                source_network="ethereum_sepolia",
                destination_network="ethereum_sepolia",
                recipient=match[2],
            )
            return self.review(payment)
        short = re.fullmatch(r"/?(approve|reject)(?:\s+(?:it|this))?[.!]?", command, re.I)
        if short:
            code = self.recent_approval_code(user)
            if not code:
                return "There isn't a clear payment review to approve in this chat. Ask me to show the payment first."
            command = f"{short[1]} {code}"
        match = re.fullmatch(r"/?(approve|reject)\s+([0-9a-fo]{12})", command, re.I)
        if match:
            # Codes are hexadecimal, so O can only mean zero. Do not fuzzy-match
            # other characters or infer which payment the user intended.
            code = match[2].upper().replace("O", "0")
            payment = next((item for item in self.wallets.list(user) if item["approval_code"] == code), None)
            if not payment:
                raise ValueError("No payment with that approval code in this chat")
            if match[1].lower() == "reject":
                self.wallets.reject(user, payment["id"])
                self.store.update_task(user, payment["task_id"], status="cancelled", notes="Payment rejected by user")
                if payment.get("kind") == "ccip":
                    return "Cross-chain send rejected. No CCIP transfer was submitted. " + (
                        "The earlier token allowance transaction remains saved."
                        if payment.get("approval_tx_hash")
                        else ""
                    )
                return "Payment rejected. No transfer was submitted."
            if (
                payment["status"] in {"awaiting_approval", "awaiting_signature"}
                and not payment.get("signing_requested_at")
                and payment["expires_at"] <= time.time()
            ):
                from agent.payments import refresh_review

                payment = await refresh_review(self.wallets, user, payment["id"])
                return (
                    "That review expired. I refreshed the same payment; please approve the new review below.\n"
                    + self.review(payment)
                )
            if payment["status"] == "awaiting_approval":
                # This event comes directly from the channel's incoming user message, never model output.
                event_id = self.store.event(
                    user,
                    "payment_approval",
                    {"intent": Wallets.fingerprint(payment), "transfer_id": payment["id"]},
                    task_id=payment["task_id"],
                    source="user",
                )
            else:
                event_id = None
            result = await self.wallets.approve(user, payment["id"], event_id)
            if result.get("confirmation_link"):
                if result.get("kind") == "ccip_deployment":
                    return (
                        "CCIP sender deployment reviewed in chat. No token transfer.\n"
                        "Open MetaMask on Ethereum Sepolia and review the contract creation and gas fee:\n"
                        f"{result['confirmation_link']}\n"
                        "I will verify the receipt and save the deployed address automatically."
                    )
                return (
                    f"Payment reviewed in chat: {result['amount']} {result['asset']} to {result['recipient']}.\n"
                    f"Payment ID: {result['id']}\n"
                    "Open MetaMask to review and sign this exact payment:\n"
                    f"{result['confirmation_link']}\n"
                    + (
                        "If MetaMask shows an older payment, paste this direct link into its browser:\n"
                        f"{result['review_link']}\n"
                        if result.get("review_link") else ""
                    )
                    + "Check the amount, token and payment ID on the review page. "
                    "I will keep checking its receipt in this chat."
                )
            return (
                f"Payment {result['id']}: {result['status']}.\n"
                f"Transaction: {result.get('tx_hash') or 'not submitted'}\n"
                "I will check the saved transaction; I will not send it again."
            )
        if re.match(r"/?(?:approve|reject)\b", command, re.I):
            return (
                "Reply approve to approve the latest payment review, or reject to cancel it. "
                "This message did not authorize a payment."
            )
        match = re.fullmatch(r"/?receipt ([a-f0-9]{32})", command, re.I)
        if match:
            result = await self.wallets.status(user, match[1])
            return (
                f"Payment {result['id']}: {result['status']}.\nTransaction: {result.get('tx_hash') or 'not submitted'}"
            )
        return None
