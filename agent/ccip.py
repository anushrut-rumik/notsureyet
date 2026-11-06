"""CCIP testnet intents. All signing is delegated to the existing Wallets authorization path."""

import secrets
import time
from decimal import Decimal, localcontext
from uuid import uuid4

from eth_abi import decode, encode
from web3 import Web3

from agent.wallet import SEPOLIA, address, amount_wei

SOURCE_SELECTOR = 16015286601757825753
DEST_SELECTOR = 10344971235874465080
BASE_SEPOLIA = 84532
SOURCE_ROUTER = "0x0BF3dE8c5D3e8A2B34D2BEeB17ABfCeBaf363A59"
DEST_ROUTER = "0xD3b06cEbF099CE7DA4AcCf578aaebFDBd6e88a93"
SOURCE_TOKEN = "0xFd57b4ddBf88a4e07fF4e34C487b99af2Fe82a05"
DEST_TOKEN = "0x88A2d74F47a237a62e7A51cdDa67270CE381555e"
# ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0)): pure tokens, finalized, defaults.
EXTRA_ARGS = "0xa69dd4aa" + "00" * 15
SENT_TOPIC = Web3.keccak(text="TransferSent(bytes32,bytes32,address,address,uint256,uint256)").to_0x_hex()
EXEC_TOPIC = Web3.keccak(text="ExecutionStateChanged(uint64,uint64,bytes32,uint8,bytes)").to_0x_hex()
TOKEN_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").to_0x_hex()


def calldata(signature, types=(), values=()):
    return "0x" + Web3.keccak(text=signature)[:4].hex() + encode(types, values).hex()


def token_units(value):
    # Token amounts can be much larger than ETH payment limits. Preserve every digit in the review and calldata.
    with localcontext() as context:
        context.prec = 80
        return amount_wei(value)


def decimal(value):
    with localcontext() as context:
        context.prec = 80
        return format(Decimal(value) / 10**18, "f")


class CCIP:
    def __init__(self, wallets):
        self.wallets = wallets
        self.store = wallets.store
        self.env = wallets.config.env
        self.destination_rpc_override = None

    def sender(self):
        value = self.env.get("AGENT_CCIP_SENDER_ADDRESS")
        if not value:
            raise ValueError("Deploy CCIPSender on Ethereum Sepolia and set AGENT_CCIP_SENDER_ADDRESS")
        return address(value)

    async def destination_rpc(self, method, params):
        if self.destination_rpc_override:
            return await self.destination_rpc_override(method, params)
        try:
            response = await self.wallets.client.post(
                self.env.get("AGENT_BASE_SEPOLIA_RPC_URL") or "https://sepolia.base.org",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise ValueError
            return payload["result"]
        except Exception:
            raise ValueError(
                "Base Sepolia RPC unavailable; delivery remains unverified. Check the saved intent again."
            ) from None

    async def read(self, target, signature, types=(), values=(), output=("uint256",), *, destination=False):
        rpc = self.destination_rpc if destination else self.wallets.rpc
        data = await rpc("eth_call", [{"to": target, "data": calldata(signature, types, values)}, "latest"])
        return decode(output, bytes.fromhex(data[2:]))[0]

    async def validate(self):
        await self.wallets.network()
        if int(await self.destination_rpc("eth_chainId", []), 16) != BASE_SEPOLIA:
            raise ValueError("Destination RPC must use Base Sepolia (84532)")
        sender = self.sender()
        if await self.wallets.rpc("eth_getCode", [sender, "latest"]) == "0x":
            raise ValueError("No CCIPSender contract at configured address")
        for getter, expected, output in (
            ("router()", SOURCE_ROUTER, ("address",)),
            ("token()", SOURCE_TOKEN, ("address",)),
            ("destination()", DEST_SELECTOR, ("uint64",)),
        ):
            result = await self.read(sender, getter, output=output)
            if str(result).lower() != str(expected).lower():
                raise ValueError("Configured CCIPSender does not match the verified Sepolia to Base Sepolia route")
        if not await self.read(SOURCE_ROUTER, "isChainSupported(uint64)", ["uint64"], [DEST_SELECTOR], ["bool"]):
            raise ValueError("CCIP Router currently reports this route unsupported")
        return sender

    async def offramps(self):
        ramps = await self.read(DEST_ROUTER, "getOffRamps()", output=["(uint64,address)[]"], destination=True)
        ramps = [address(ramp) for selector, ramp in ramps if selector == SOURCE_SELECTOR]
        if not ramps:
            raise ValueError("No authorized Base Sepolia OffRamp for Ethereum Sepolia")
        return ramps

    async def quote(self, user, recipient, amount):
        recipient, units = address(recipient), token_units(amount)
        sender = await self.validate()
        wallet = self.wallets.get(user)
        fee = await self.read(
            sender,
            "quote(address,uint256,bytes)",
            ["address", "uint256", "bytes"],
            [recipient, units, bytes.fromhex(EXTRA_ARGS[2:])],
        )
        cap = (fee * 110 + 99) // 100
        maximum = amount_wei(self.env.get("AGENT_CCIP_MAX_FEE_ETH") or "0.01")
        if cap > maximum:
            raise ValueError("CCIP fee exceeds AGENT_CCIP_MAX_FEE_ETH")
        allowance = await self.read(
            SOURCE_TOKEN, "allowance(address,address)", ["address", "address"], [wallet["address"], sender]
        )
        balance = await self.read(SOURCE_TOKEN, "balanceOf(address)", ["address"], [wallet["address"]])
        return {
            "sender": wallet["address"],
            "recipient": recipient,
            "amount": decimal(units),
            "amount_units": str(units),
            "asset": "CCIP-BnM",
            "source": "Ethereum Sepolia",
            "destination": "Base Sepolia",
            "destination_chain_id": BASE_SEPOLIA,
            "destination_selector": str(DEST_SELECTOR),
            "source_token": SOURCE_TOKEN,
            "destination_token": DEST_TOKEN,
            "sender_contract": sender,
            "extra_args": EXTRA_ARGS,
            "ccip_fee_wei": str(fee),
            "ccip_fee_eth": decimal(fee),
            "ccip_fee_cap_wei": str(cap),
            "ccip_fee_cap_eth": decimal(cap),
            "allowance_required": allowance < units,
            "token_balance_units": str(balance),
            "quoted_at": time.time(),
            "note": "Quote only. Native source gas is additional; no tokens have been sent.",
        }

    async def prepare(self, user, task_id, recipient, amount):
        async with self.wallets.lock:
            if self.store.backend != "arkiv":
                raise ValueError("CCIP transfers require AGENT_TASK_BACKEND=arkiv")
            task = self.store.task(user, task_id)
            if task["status"] in {"completed", "cancelled"}:
                raise ValueError("Cannot transfer from an ended task")
            recipient, units = address(recipient), token_units(amount)
            for existing in self.wallets.list(user):
                if existing["task_id"] == task_id:
                    if (
                        existing.get("kind") != "ccip"
                        or existing["recipient"] != recipient
                        or int(existing["amount_units"]) != units
                    ):
                        raise ValueError("This task already has a different payment intent")
                    return existing
            wallet = self.wallets.get(user)
            if wallet["kind"] not in {"local", "metamask"}:
                raise ValueError("Connect MetaMask or create an agent wallet before preparing CCIP")
            quote = await self.quote(user, recipient, amount)
            if int(quote["token_balance_units"]) < units:
                raise ValueError("Insufficient CCIP-BnM on Ethereum Sepolia; fund the connected wallet")
            ramps = await self.offramps()
            transfer = {
                **quote,
                "id": uuid4().hex,
                "user_id": user,
                "task_id": task_id,
                "destination_offramps": ramps,
                "kind": "ccip",
                "chain_id": SEPOLIA,
                "wallet_kind": wallet["kind"],
                "intent_id": "0x" + secrets.token_hex(32),
                "message_id": None,
                "source_tx_hash": None,
                "approval_tx_hash": None,
                "created_at": time.time(),
                "destination_start_block": int(await self.destination_rpc("eth_blockNumber", []), 16),
            }
            await self.prepare_step(transfer, "allowance" if quote["allowance_required"] else "send")
            return self.wallets.view(transfer)

    async def refresh(self, user, transfer_id):
        async with self.wallets.lock:
            transfer = self.wallets.load(user, transfer_id)
            if (
                transfer.get("kind") != "ccip"
                or transfer["status"] not in {"awaiting_approval", "awaiting_signature"}
                or transfer.get("tx_hash")
                or transfer.get("signing_requested_at")
            ):
                raise ValueError("Only an unsigned CCIP review can be refreshed; inspect the existing transaction")
            task = self.store.task(user, transfer["task_id"])
            if task["status"] in {"completed", "cancelled"}:
                raise ValueError("This task has ended")
            wallet = self.wallets.get(user)
            if wallet["address"] != transfer["sender"] or wallet["kind"] != transfer["wallet_kind"]:
                raise ValueError("Wallet changed after preparation")
            await self.prepare_step(transfer, transfer["phase"])
            return self.wallets.view(transfer)

    async def prepare_step(self, transfer, phase):
        sender = transfer["sender_contract"]
        units = int(transfer["amount_units"])
        if phase == "allowance":
            to, value = SOURCE_TOKEN, 0
            data = calldata("approve(address,uint256)", ["address", "uint256"], [sender, units])
        else:
            quote = await self.quote(transfer["user_id"], transfer["recipient"], transfer["amount"])
            transfer.update(
                {k: quote[k] for k in ("ccip_fee_wei", "ccip_fee_eth", "ccip_fee_cap_wei", "ccip_fee_cap_eth")}
            )
            to, value = sender, int(transfer["ccip_fee_cap_wei"])
            data = calldata(
                "send(bytes32,address,uint256,uint256,bytes)",
                ["bytes32", "address", "uint256", "uint256", "bytes"],
                [
                    bytes.fromhex(transfer["intent_id"][2:]),
                    transfer["recipient"],
                    units,
                    value,
                    bytes.fromhex(transfer["extra_args"][2:]),
                ],
            )
        tx = {"from": transfer["sender"], "to": to, "value": hex(value), "data": data}
        rpc = self.wallets.rpc
        gas = (int(await rpc("eth_estimateGas", [tx, "latest"]), 16) * 12 + 9) // 10
        price = int(await rpc("eth_gasPrice", []), 16)
        if int(await rpc("eth_getBalance", [transfer["sender"], "latest"]), 16) < value + gas * price:
            raise ValueError("Insufficient Sepolia ETH for the CCIP fee and source transaction gas")
        tx.update(
            chainId=hex(SEPOLIA),
            nonce=await rpc("eth_getTransactionCount", [transfer["sender"], "pending"]),
            gas=hex(gas),
            gasPrice=hex(price),
        )
        transfer.update(
            phase=phase,
            transaction=tx,
            value_wei=str(value),
            max_fee_wei=str(gas * price),
            tx_hash=None,
            status="awaiting_approval",
            expires_at=time.time() + 300,
            approval_code=secrets.token_hex(6).upper(),
            start_block=int(await rpc("eth_blockNumber", []), 16),
        )
        for key in ("raw", "signing_requested_at", "approved_at"):
            transfer.pop(key, None)
        self.wallets.save(transfer)

    async def source_receipt(self, transfer, receipt):
        if int(receipt["status"], 16) != 1:
            transfer["status"] = "reverted"
            if transfer["phase"] == "send":
                transfer["source_tx_hash"] = transfer["tx_hash"]
        elif transfer["phase"] == "allowance":
            transfer["approval_tx_hash"] = transfer["tx_hash"]
            # Persist the completed allowance before any new RPC or preview generation.
            transfer["status"] = "allowance_confirmed"
            self.wallets.save(transfer)
            return await self.advance(transfer)
        else:
            logs = [
                log
                for log in receipt["logs"]
                if log["address"].lower() == transfer["sender_contract"].lower()
                and len(log["topics"]) == 4
                and log["topics"][0].lower() == SENT_TOPIC.lower()
                and log["topics"][1].lower() == transfer["intent_id"].lower()
                and int(log["topics"][3], 16) == int(transfer["sender"], 16)
            ]
            if len(logs) != 1:
                raise ValueError("Source receipt has no unique matching CCIP TransferSent event")
            recipient, amount, fee = decode(["address", "uint256", "uint256"], bytes.fromhex(logs[0]["data"][2:]))
            if (
                recipient.lower() != transfer["recipient"].lower()
                or amount != int(transfer["amount_units"])
                or fee > int(transfer["ccip_fee_cap_wei"])
            ):
                raise ValueError("CCIP event differs from approved transfer")
            transfer.update(
                status="in_transit",
                message_id=logs[0]["topics"][2],
                source_tx_hash=transfer["tx_hash"],
                source_block=receipt["blockNumber"],
                source_block_hash=receipt["blockHash"],
                actual_ccip_fee_wei=str(fee),
            )
        self.wallets.save(transfer)
        return self.wallets.view(transfer)

    async def advance(self, transfer):
        # Recovery can repeat preview preparation; this never signs or broadcasts.
        await self.prepare_step(transfer, "send")
        from agent.chat_commands import ChatCommands

        self.store.notice(
            transfer["user_id"],
            "Token allowance confirmed. Review the cross-chain send:\n" + ChatCommands.review(transfer),
            transfer["task_id"],
        )
        return self.wallets.view(transfer)

    async def delivery(self, transfer):
        rpc = self.destination_rpc
        if int(await rpc("eth_chainId", []), 16) != BASE_SEPOLIA:
            raise ValueError("Wrong destination network")
        # Source acceptance is not delivery. Wait for canonical finalized source inclusion too.
        finalized = await self.wallets.rpc("eth_getBlockByNumber", ["finalized", False])
        if int(finalized["number"], 16) < int(transfer["source_block"], 16):
            return self.wallets.view(transfer)
        block = await self.wallets.rpc("eth_getBlockByNumber", [transfer["source_block"], False])
        if block["hash"].lower() != transfer["source_block_hash"].lower():
            raise ValueError("Source transaction reorganized; reconcile saved hash, never resend")
        ramps = await self.offramps()
        # Retain previously observed ramps so an upgrade cannot discard an in-flight message.
        ramps = sorted(set(transfer.get("destination_offramps", []) + ramps))
        if not ramps:
            raise ValueError("No authorized Base Sepolia OffRamp for Ethereum Sepolia")
        end = int((await rpc("eth_getBlockByNumber", ["finalized", False]))["number"], 16)
        start = transfer.get("destination_cursor", transfer["destination_start_block"])
        if start > end:
            return self.wallets.view(transfer)
        stop = min(start + 1999, end)
        logs = await rpc(
            "eth_getLogs",
            [
                {
                    "address": ramps,
                    "fromBlock": hex(start),
                    "toBlock": hex(stop),
                    "topics": [
                        EXEC_TOPIC,
                        "0x" + SOURCE_SELECTOR.to_bytes(32, "big").hex(),
                        None,
                        transfer["message_id"],
                    ],
                }
            ],
        )
        for log in logs:
            if (
                log.get("removed")
                or log["address"].lower() not in {r.lower() for r in ramps}
                or len(log["topics"]) != 4
                or log["topics"][0].lower() != EXEC_TOPIC.lower()
                or int(log["topics"][1], 16) != SOURCE_SELECTOR
                or log["topics"][3].lower() != transfer["message_id"].lower()
            ):
                raise ValueError("Destination RPC returned an unrelated execution event")
            state, _ = decode(["uint8", "bytes"], bytes.fromhex(log["data"][2:]))
            if state != 2:
                transfer["last_execution_state"] = state
                continue  # Failed execution can later be manually retried, without another source send.
            receipt = await rpc("eth_getTransactionReceipt", [log["transactionHash"]])
            canonical = await rpc("eth_getBlockByNumber", [log["blockNumber"], False])
            if (
                not receipt
                or int(receipt["status"], 16) != 1
                or receipt["transactionHash"].lower() != log["transactionHash"].lower()
                or receipt["blockHash"].lower() != log["blockHash"].lower()
                or receipt["blockNumber"] != log["blockNumber"]
                or canonical["hash"].lower() != log["blockHash"].lower()
                or int(log["blockNumber"], 16) > end
            ):
                raise ValueError("Destination execution receipt is not canonical and finalized")
            # Also require the token movement to the intended recipient in that execution transaction.
            moved = sum(
                int(item["data"], 16)
                for item in receipt["logs"]
                if item["address"].lower() == DEST_TOKEN.lower()
                and len(item["topics"]) == 3
                and item["topics"][0].lower() == TOKEN_TOPIC.lower()
                and int(item["topics"][2], 16) == int(transfer["recipient"], 16)
            )
            if moved < int(transfer["amount_units"]):
                raise ValueError("Destination execution has no matching CCIP-BnM delivery")
            transfer.update(
                status="delivered",
                destination_tx_hash=log["transactionHash"],
                destination_block=log["blockNumber"],
                delivered_at=time.time(),
            )
            self.wallets.save(transfer)
            self.store.notice(
                transfer["user_id"],
                f"Delivered {transfer['amount']} CCIP-BnM to {transfer['recipient']} on Base Sepolia.\n"
                f"Source transaction: {transfer['source_tx_hash']}\nCCIP message: {transfer['message_id']}\n"
                f"Destination transaction: {transfer['destination_tx_hash']}",
                transfer["task_id"],
            )
            task = self.store.task(transfer["user_id"], transfer["task_id"])
            if task["status"] in {"needs_input", "waiting"}:
                self.store.update_task(transfer["user_id"], task["id"], status="queued", wake_at=None)
            return self.wallets.view(transfer)
        # Routine polling is read-only. Avoid requesting a MetaMask Arkiv signature every few blocks.
        # Persist only a full scan page, an execution observation, or a changed authorized ramp set.
        if stop < end or logs or set(ramps) != set(transfer.get("destination_offramps", [])):
            transfer.update(destination_cursor=stop + 1, destination_offramps=ramps)
            self.wallets.save(transfer)
        return self.wallets.view(transfer)


def register_ccip_tools(registry):
    @registry.tool()
    async def quote_ccip_fee(ctx, recipient: str, amount: str):
        """Quote native ETH fees for CCIP-BnM from Ethereum Sepolia to Base Sepolia. Does not sign."""
        return await ctx.engine.wallets.ccip.quote(ctx.user_id, recipient, amount)

    @registry.tool(modes=("task",))
    async def prepare_ccip_transfer(ctx, recipient: str, amount: str):
        """Prepare or resume this task's single CCIP intent. Show review_message; pause for exact approval.
        An allowance needs a separate review before the send. Never create a new task to retry a transfer.
        """
        from agent.chat_commands import ChatCommands

        result = await ctx.engine.wallets.ccip.prepare(ctx.user_id, ctx.task_id, recipient, amount)
        return {**result, "review_message": ChatCommands.review(result)}

    @registry.tool(modes=("task",))
    async def refresh_ccip_review(ctx, transfer_id: str):
        """Refresh an expired or fee-stale UNSIGNED review on the same intent. Requires fresh approval.
        Refuses after any signing request or submission; then track the saved transaction instead.
        """
        from agent.chat_commands import ChatCommands

        transfer = ctx.engine.wallets.load(ctx.user_id, transfer_id)
        if transfer["task_id"] != ctx.task_id:
            raise ValueError("This intent belongs to another task")
        result = await ctx.engine.wallets.ccip.refresh(ctx.user_id, transfer_id)
        return {**result, "review_message": ChatCommands.review(result)}

    @registry.tool()
    async def ccip_transfer_status(ctx, transfer_id: str):
        """Check the saved source transaction, message ID and finalized destination delivery. Never resends."""
        transfer = ctx.engine.wallets.load(ctx.user_id, transfer_id)
        if transfer.get("kind") != "ccip":
            raise ValueError("This is not a CCIP intent")
        result = await ctx.engine.wallets.status(ctx.user_id, transfer_id)
        return {**result, "delivered": result["status"] == "delivered"}

    @registry.tool(evidence=True, modes=("task",))
    async def ccip_delivery_receipt(ctx, transfer_id: str):
        """Completion evidence only after finalized delivery of this task's CCIP message on Base Sepolia."""
        transfer = ctx.engine.wallets.load(ctx.user_id, transfer_id)
        if transfer.get("kind") != "ccip" or transfer["task_id"] != ctx.task_id:
            raise ValueError("No CCIP transfer for this task")
        result = await ctx.engine.wallets.status(ctx.user_id, transfer_id)
        if result["status"] != "delivered":
            raise ValueError("CCIP is not delivered; keep tracking the saved intent")
        return {**result, "delivered": True}
