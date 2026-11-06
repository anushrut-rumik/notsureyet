"""Prepare the requested payment in chat before asking for any storage transaction."""

import json
import secrets
import time
from typing import Literal

from agent.arkiv import ArkivStore
from agent.ccip_simulation import mode as ccip_mode
from agent.chat_commands import ChatCommands
from agent.store import Store
from agent.wallet import SEPOLIA, address, amount_wei

Network = Literal["ethereum_sepolia", "base_sepolia", "arkiv_rabdi"]
Asset = Literal["ETH", "tGLM", "CCIP-BnM"]
NETWORK_NAMES = {"ethereum_sepolia": "Ethereum Sepolia", "base_sepolia": "Base Sepolia", "arkiv_rabdi": "Arkiv Rabdi"}


def capabilities(wallets):
    simulation = ccip_mode(wallets.config) == "simulation"
    return {
        "native": ["ETH: Ethereum Sepolia -> Ethereum Sepolia", "tGLM: Arkiv Rabdi -> Arkiv Rabdi"],
        "ccip": {
            "mode": "simulation" if simulation else "testnet",
            "asset": "CCIP-BnM",
            "source": "ethereum_sepolia",
            "destination": "base_sepolia",
            "configured": simulation or bool(wallets.config.env.get("AGENT_CCIP_SENDER_ADDRESS")),
            "execution": (
                "Chainlink Local Simulator; no signing or public transfer" if simulation else "Public testnets"
            ),
            "missing_setting": None
            if simulation or wallets.config.env.get("AGENT_CCIP_SENDER_ADDRESS")
            else "AGENT_CCIP_SENDER_ADDRESS (deployed CCIPSender on Ethereum Sepolia)",
        },
        "approval": "prepare_payment validates first and returns an unsigned review; storage approval is separate",
    }


async def prepare_request(
    wallets, user, *, asset, amount, source_network, destination_network, recipient="", task_id=""
):
    routes = {
        ("ETH", "ethereum_sepolia", "ethereum_sepolia"): SEPOLIA,
        ("tGLM", "arkiv_rabdi", "arkiv_rabdi"): 7794095,
        ("CCIP-BnM", "ethereum_sepolia", "base_sepolia"): SEPOLIA,
    }
    route = (asset, source_network, destination_network)
    if route not in routes:
        raise ValueError(
            "Unsupported payment route. Supported: ETH within Ethereum Sepolia, tGLM within Arkiv Rabdi, "
            "or CCIP-BnM from Ethereum Sepolia to Base Sepolia. Specify the intended source and destination."
        )
    from agent.ccip import token_units

    units = token_units(amount) if asset == "CCIP-BnM" else amount_wei(amount, asset=asset)
    wallet = wallets.get(user)
    recipient = address(recipient) if recipient else wallets.agent_wallet(user)["address"]
    if asset == "CCIP-BnM" and ccip_mode(wallets.config) == "simulation":
        from agent.ccip_simulation import simulate

        if task_id:
            raise ValueError("Local simulations are separate from payment tasks; omit task_id to run a simulation")
        return await simulate(wallets, user, recipient, amount)
    if wallet["kind"] not in {"metamask", "local"}:
        raise ValueError("Connect a signing wallet before preparing a payment")
    store = wallets.store
    if not isinstance(store, ArkivStore) or not store.available:
        raise ValueError("Arkiv task storage must be configured and recovered before preparing a payment")
    for existing in wallets.list(user):
        if task_id and existing["task_id"] != task_id:
            continue
        matches = (
            existing["sender"].lower() == wallet["address"].lower()
            and existing["recipient"] == recipient
            and existing["asset"] == asset
            and existing["chain_id"] == routes[route]
            and int(existing.get("amount_units", existing["value_wei"])) == units
        )
        if matches:
            if task_id or existing["status"] not in {"rejected", "reverted", "confirmed", "delivered"}:
                if (
                    existing["status"] in {"awaiting_approval", "awaiting_signature"}
                    and not existing.get("signing_requested_at")
                    and not existing.get("tx_hash")
                    and time.time() > existing["expires_at"]
                ):
                    return await refresh_review(wallets, user, existing["id"])
                return existing
        elif task_id:
            raise ValueError("This task already has a different payment intent; inspect it instead of replacing it")
    # An existing transfer remains trackable even when configuration changes after admission.
    if asset == "CCIP-BnM":
        wallets.ccip.sender()
    # Validate balance, route and fee configuration before even creating a local draft task.
    if asset == "CCIP-BnM":
        quote = await wallets.ccip.quote(user, recipient, amount)
        if int(quote["token_balance_units"]) < units:
            raise ValueError("Insufficient CCIP-BnM on Ethereum Sepolia")
    else:
        limit = "AGENT_MAX_TRANSFER_ETH" if asset == "ETH" else "AGENT_MAX_TRANSFER_TGLM"
        if units > amount_wei(wallets.config.env.get(limit) or "0.01", asset=asset):
            raise ValueError("Transfer exceeds " + limit)
        balance = await (wallets.balance(user) if asset == "ETH" else wallets.rabdi.balance(user))
        if int(balance["balance_wei"]) <= units:
            raise ValueError(f"Insufficient {asset} for the requested amount and network fee")
    request = {
        "asset": asset,
        "amount": amount,
        "source_network": source_network,
        "destination_network": destination_network,
        "recipient": recipient,
    }
    if task_id:
        task = store.task(user, task_id)
        if task["status"] in {"completed", "cancelled"}:
            raise ValueError("This task has ended")
    else:
        # These are unsigned local drafts. Financial authority is unchanged: approval persists the
        # exact intent on Arkiv before a signer is admitted. No storage transaction is created here.
        task = Store.create_task(
            store,
            user,
            f"Send {amount} {asset} to {NETWORK_NAMES[destination_network]}",
            json.dumps({"payment_request": request}),
            "Verified payment delivery",
        )
        task_id = task["id"]
    with store.stage_checkpoint(task_id):
        Store.update_task(
            store,
            user,
            task_id,
            status="needs_input",
            wake_at=None,
            notes="Preparing an unsigned payment review; no payment or storage signature requested",
        )
        if asset == "CCIP-BnM":
            payment = await wallets.ccip.prepare(user, task_id, recipient, amount)
        elif asset == "tGLM":
            payment = await wallets.rabdi.prepare(user, task_id, recipient, amount)
        else:
            payment = await wallets.prepare(user, task_id, recipient, amount)
        payment = wallets.load(user, payment["id"])
        payment["payment_request"] = request
        wallets.save(payment)
        Store.update_task(store, user, task_id, notes=f"Awaiting explicit payment approval for {payment['id']}")
    return wallets.view(payment)


async def refresh_review(wallets, user, transfer_id):
    payment = wallets.load(user, transfer_id)
    if (
        payment["status"] not in {"awaiting_approval", "awaiting_signature"}
        or payment.get("tx_hash")
        or payment.get("signing_requested_at")
    ):
        raise ValueError("A signing request already started; track this existing payment instead of refreshing it")
    task = wallets.store.task(user, payment["task_id"])
    wallet = wallets.get(user)
    if task["status"] in {"completed", "cancelled"}:
        raise ValueError("This task has ended")
    if wallet["address"] != payment["sender"] or wallet["kind"] != payment["wallet_kind"]:
        raise ValueError("Wallet changed after preparation")
    with wallets.store.stage_checkpoint(payment["task_id"]):
        if payment.get("kind") == "ccip":
            return await wallets.ccip.refresh(user, transfer_id)
        async with wallets.lock:
            # Reload after taking the signing lock: a claim may have started while waiting.
            payment = wallets.load(user, transfer_id)
            if payment.get("tx_hash") or payment.get("signing_requested_at"):
                raise ValueError("This payment was already admitted; inspect its saved transaction")
            rpc = wallets.rabdi.rpc if payment.get("kind") == "rabdi_native" else wallets.rpc
            await (wallets.rabdi.network() if payment.get("kind") == "rabdi_native" else wallets.network())
            tx = {k: v for k, v in payment["transaction"].items() if k not in {"gas", "gasPrice", "nonce"}}
            gas = (int(await rpc("eth_estimateGas", [tx, "latest"]), 16) * 12 + 9) // 10
            price = int(await rpc("eth_gasPrice", []), 16)
            if (
                int(await rpc("eth_getBalance", [payment["sender"], "latest"]), 16)
                < int(payment["value_wei"]) + gas * price
            ):
                raise ValueError("Insufficient balance for the payment and refreshed network fee")
            tx.update(gas=hex(gas), gasPrice=hex(price))
            if payment.get("kind") != "rabdi_native":
                tx["nonce"] = await rpc("eth_getTransactionCount", [payment["sender"], "pending"])
            payment.update(
                transaction=tx,
                status="awaiting_approval",
                max_fee_wei=str(gas * price),
                expires_at=time.time() + 300,
                approval_code=secrets.token_hex(6).upper(),
            )
            payment.pop("approved_at", None)
            wallets.save(payment)
            return wallets.view(payment)


def register_payment_tools(registry):
    @registry.tool(modes=("chat",))
    async def prepare_payment(
        ctx,
        asset: Asset,
        amount: str,
        source_network: Network,
        destination_network: Network,
        recipient: str = "",
        task_id: str = "",
    ):
        """Prepare or resume a payment directly in chat. Use this INSTEAD OF create_task for payment requests.
        Preserve the user's asset, amount, source and destination. Empty recipient means this chat's agent wallet.
        Validates setup before requesting storage. No signing or sending. Returns an exact approval review.
        Only pass task_id when resuming that specific payment task, never to repurpose an unrelated task.
        """
        try:
            payment = await prepare_request(
                ctx.engine.wallets,
                ctx.user_id,
                asset=asset,
                amount=amount,
                source_network=source_network,
                destination_network=destination_network,
                recipient=recipient,
                task_id=task_id,
            )
        except ValueError as exc:
            ctx.payment_response = f"Cannot prepare this payment: {exc}. No payment was signed or sent."
            raise
        if payment.get("kind") == "ccip_simulation":
            from agent.ccip_simulation import review as simulation_review

            ctx.payment_response = simulation_review(payment)
            return {**payment, "review_message": ctx.payment_response, "storage_status": "not_required_for_simulation"}
        if payment["status"] in {"awaiting_approval", "awaiting_signature"}:
            review = ChatCommands.review(payment)
        else:
            review = (
                f"Existing {payment['asset']} payment {payment['id']}: {payment['status']}. "
                f"Transaction: {payment.get('tx_hash') or 'not submitted'}. This request was not sent again."
            )
        ctx.payment_response = review
        return {**payment, "review_message": review, "storage_status": "local_draft_until_approval"}

    @registry.tool(modes=("chat",))
    async def refresh_payment_review(ctx, transfer_id: str):
        """Refresh an expired UNSIGNED review on the same payment and task, requiring fresh approval.
        Never use after a wallet signing request or submission. Inspect the saved payment instead.
        """
        result = await refresh_review(ctx.engine.wallets, ctx.user_id, transfer_id)
        ctx.payment_response = ChatCommands.review(result)
        return {**result, "review_message": ctx.payment_response}
