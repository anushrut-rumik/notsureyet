"""Prepare a CCIP sender deployment using the existing chat approval and wallet signer."""

import json
import os
import re
import secrets
import tempfile
import time
from uuid import uuid4

import rlp
from eth_abi import encode
from web3 import Web3

from agent.arkiv import ArkivStore
from agent.ccip import DEST_SELECTOR, SOURCE_ROUTER, SOURCE_TOKEN, decimal
from agent.store import Store
from agent.wallet import SEPOLIA, address, amount_wei


def contract_address(sender, nonce):
    return address(Web3.keccak(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[-20:].hex())


async def prepare(wallets, user):
    from agent.ccip_simulation import mode

    if mode(wallets.config) != "testnet":
        raise ValueError("Switch AGENT_CCIP_MODE to testnet before preparing a public deployment")
    if wallets.config.env.get("AGENT_CCIP_SENDER_ADDRESS"):
        return {"status": "configured", "sender_contract": await wallets.ccip.validate()}
    async with wallets.lock:
        for payment in wallets.list(user):
            if payment.get("kind") == "ccip_deployment" and payment["status"] not in {"rejected", "reverted"}:
                return payment
        wallet = wallets.get(user)
        if wallet["kind"] != "metamask":
            raise ValueError("Connect MetaMask to deploy the sender from your existing wallet")
        if not isinstance(wallets.store, ArkivStore) or not wallets.store.available:
            raise ValueError("Recover Arkiv task storage before preparing a deployment")
        await wallets.network()
        if not await wallets.ccip.read(
            SOURCE_ROUTER, "isChainSupported(uint64)", ["uint64"], [DEST_SELECTOR], ["bool"]
        ):
            raise ValueError("The official CCIP Router reports the destination unsupported")
        artifact_path = wallets.config.root / "contracts/out/CCIPSender.sol/CCIPSender.json"
        if not artifact_path.exists():
            raise ValueError("Build the sender first with npm run ccip:build")
        bytecode = json.loads(artifact_path.read_text())["bytecode"]["object"]
        code = bytes.fromhex(bytecode.removeprefix("0x"))
        data = (
            "0x" + (code + encode(["address", "address", "uint64"], [SOURCE_ROUTER, SOURCE_TOKEN, DEST_SELECTOR])).hex()
        )
        rpc = wallets.rpc
        nonce = int(await rpc("eth_getTransactionCount", [wallet["address"], "pending"]), 16)
        tx = {"from": wallet["address"], "value": "0x0", "data": data}
        gas = (int(await rpc("eth_estimateGas", [tx, "latest"]), 16) * 12 + 9) // 10
        price = int(await rpc("eth_gasPrice", []), 16) * 2
        fee = gas * price
        if fee > amount_wei(wallets.config.env.get("AGENT_CCIP_MAX_DEPLOYMENT_FEE_ETH") or "0.005"):
            raise ValueError("Deployment fee exceeds AGENT_CCIP_MAX_DEPLOYMENT_FEE_ETH")
        if int(await rpc("eth_getBalance", [wallet["address"], "latest"]), 16) < fee:
            raise ValueError("Insufficient Sepolia ETH for the reviewed deployment gas")
        tx.update(chainId=hex(SEPOLIA), nonce=hex(nonce), gas=hex(gas), gasPrice=hex(price))
        task = Store.create_task(
            wallets.store,
            user,
            "Deploy CCIP sender on Ethereum Sepolia",
            "Deploy the compiled CCIPSender with the official Sepolia Router and CCIP-BnM; destination Base Sepolia.",
            "Canonical successful creation receipt and verified immutable route configuration",
        )
        with wallets.store.stage_checkpoint(task["id"]):
            Store.update_task(wallets.store, user, task["id"], status="needs_input", wake_at=None)
            payment = {
                "id": uuid4().hex,
                "user_id": user,
                "task_id": task["id"],
                "kind": "ccip_deployment",
                "chain_id": SEPOLIA,
                "wallet_kind": wallet["kind"],
                "sender": wallet["address"],
                "recipient": contract_address(wallet["address"], nonce),
                "asset": "ETH",
                "amount": "0",
                "value_wei": "0",
                "max_fee_wei": str(fee),
                "transaction": tx,
                "creation_code_hash": Web3.keccak(hexstr=data).to_0x_hex(),
                "status": "awaiting_approval",
                "tx_hash": None,
                "created_at": time.time(),
                "expires_at": time.time() + 300,
                "approval_code": secrets.token_hex(6).upper(),
                "start_block": int(await rpc("eth_blockNumber", []), 16),
            }
            wallets.save(payment)
        return wallets.view(payment)


def configure(config, sender):
    """Save only a receipt-verified sender, preserving the existing environment and its secrets."""
    sender = address(sender)
    env_file = config.root / ".env"
    content = env_file.read_text() if env_file.exists() else ""
    for key, value in {"AGENT_CCIP_MODE": "testnet", "AGENT_CCIP_SENDER_ADDRESS": sender}.items():
        if re.search(rf"^{key}=", content, re.M):
            content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.M)
        else:
            content += f"\n{key}={value}\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=config.root, prefix=".env-ccip-", delete=False) as output:
        temporary = output.name
        try:
            os.chmod(temporary, 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            os.unlink(temporary)
            raise
    os.replace(temporary, env_file)
    config.env.update(AGENT_CCIP_MODE="testnet", AGENT_CCIP_SENDER_ADDRESS=sender)


async def confirmed(wallets, payment, receipt):
    if int(receipt["status"], 16) != 1:
        payment["status"] = "reverted"
        wallets.save(payment)
        return wallets.view(payment)
    deployed = address(receipt.get("contractAddress") or "")
    if deployed != payment["recipient"]:
        raise ValueError("Deployment receipt has an unexpected contract address; no configuration was changed")
    if await wallets.rpc("eth_getCode", [deployed, "latest"]) == "0x":
        raise ValueError("No deployed contract code at the receipt address")
    for signature, expected, output in (
        ("router()", SOURCE_ROUTER, ["address"]),
        ("token()", SOURCE_TOKEN, ["address"]),
        ("destination()", DEST_SELECTOR, ["uint64"]),
    ):
        actual = await wallets.ccip.read(deployed, signature, output=output)
        if str(actual).lower() != str(expected).lower():
            raise ValueError("Deployed sender route mismatch; no configuration was changed")
    configure(wallets.config, deployed)
    payment.update(status="confirmed", sender_contract=deployed, block=int(receipt["blockNumber"], 16))
    Store.update_task(wallets.store, payment["user_id"], payment["task_id"], status="completed", wake_at=None)
    wallets.save(payment)
    wallets.store.notice(
        payment["user_id"],
        f"CCIP sender deployed and verified on Ethereum Sepolia: {deployed}\n"
        f"Deployment transaction: {payment['tx_hash']}\n"
        "Real testnet mode is configured. No CCIP tokens have been sent. "
        "Ask to send your chosen CCIP-BnM amount to the recipient on Base Sepolia; "
        "the token allowance and cross-chain send each require a separate review and approval.",
        payment["task_id"],
    )
    return wallets.view(payment)


def review(payment):
    return (
        f"Review CCIP sender DEPLOYMENT {payment['id']}\n"
        "Network: Ethereum Sepolia (11155111)\n"
        f"Deploying wallet: {payment['sender']}\n"
        f"Expected new contract: {payment['recipient']}\n"
        "Operation: create CCIPSender; no recipient payment and no token transfer.\n"
        f"Constructor Router: {SOURCE_ROUTER}\nToken: {SOURCE_TOKEN}\n"
        f"Destination: Base Sepolia (selector {DEST_SELECTOR})\n"
        f"Maximum deployment gas fee: {decimal(int(payment['max_fee_wei']))} ETH\n"
        "Transaction value: 0 ETH. Only the deployment gas is charged.\n"
        "Expires in 5 minutes. After verification the agent saves the contract address automatically.\n"
        f"Reply exactly: approve {payment['approval_code']}\nOr: reject {payment['approval_code']}"
    )


def register_deployment_tools(registry):
    @registry.tool(modes=("chat",))
    async def prepare_ccip_sender_deployment(ctx):
        """Prepare or resume public CCIPSender contract creation on Ethereum Sepolia from connected MetaMask.
        No token payment. Reuses raw chat approval and the existing signer; no private-key export or funding task.
        """
        payment = await prepare(ctx.engine.wallets, ctx.user_id)
        if payment["status"] == "configured":
            ctx.payment_response = f"CCIP sender already verified: {payment['sender_contract']} on Ethereum Sepolia."
        elif payment["status"] in {"awaiting_approval", "awaiting_signature"}:
            from agent.payments import refresh_review

            if time.time() > payment["expires_at"] and not payment.get("signing_requested_at"):
                payment = await refresh_review(ctx.engine.wallets, ctx.user_id, payment["id"])
            ctx.payment_response = review(payment)
        else:
            ctx.payment_response = (
                f"Existing sender deployment: {payment['status']}. Transaction: {payment.get('tx_hash')}. "
                "Track this transaction; no second deployment was prepared."
            )
        return payment
