"""Execute the real sender with Chainlink Local; never sign or contact a public chain."""

import asyncio
import hashlib
import json
import re

from eth_abi import decode

from agent.arkiv import Bridge
from agent.ccip import decimal, token_units
from agent.store import Store
from agent.wallet import address


def mode(config):
    value = config.env.get("AGENT_CCIP_MODE") or "testnet"
    if value not in {"testnet", "simulation"}:
        raise ValueError("AGENT_CCIP_MODE must be simulation or testnet")
    return value


async def execute(config, sender, recipient, units, intent):
    command = [
        "node",
        "scripts/foundry.mjs",
        "forge",
        "script",
        "contracts/script/SimulateCCIPTransfer.s.sol:SimulateCCIPTransfer",
        "--json",
        "--sig",
        "run(address,address,uint256,bytes32)",
        sender,
        recipient,
        str(units),
        "0x" + intent,
    ]
    # Fixed arguments: no RPC, fork, broadcast, private key, or user-controlled shell command.
    process = await asyncio.create_subprocess_exec(
        *command, cwd=config.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(45):
            stdout, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError("Local CCIP simulation failed. Run npm run ccip:simulate to inspect it; no funds moved")
    try:
        result = next(json.loads(line) for line in stdout.decode().splitlines() if line.startswith('{"logs":'))
        if not result["success"]:
            raise ValueError
        report = bytes.fromhex(result["returns"]["report"]["value"][2:])
        contract, message, fee, before, after, selector, duplicate = decode(
            ["address", "bytes32", "uint256", "uint256", "uint256", "uint64", "bool"], report
        )
        expected = 0 if sender.lower() == recipient.lower() else units
        if not any(message) or not duplicate or after - before != expected:
            raise ValueError
    except (KeyError, ValueError, StopIteration, UnicodeError) as exc:
        raise ValueError("Local simulator returned no verified delivery result; no public transfer occurred") from exc
    return {
        "simulator_sender_contract": address(contract),
        "simulation_message_id": "0x" + message.hex(),
        "simulated_fee_eth": decimal(fee),
        "recipient_balance_before": decimal(before),
        "recipient_balance_after": decimal(after),
        "simulator_selector": str(selector),
        "duplicate_rejected": duplicate,
    }


def path(wallets, simulation_id):
    if not re.fullmatch(r"[a-f0-9]{64}", simulation_id):
        raise ValueError("Invalid simulation reference")
    return wallets.config.data_dir / f"ccip-simulation-{simulation_id}.json"


def stored_result(wallets, user, simulation_id):
    if wallets.store.backend == "arkiv":
        for task in wallets.store.tasks(user, protocol="chainlink_local"):
            progress = wallets.store.progress(user, task["id"])
            for event in progress["events"]:
                data = event.get("data", {})
                if data.get("tool") == "ccip_simulation_result" and data["output"]["id"] == simulation_id:
                    return {**data["output"], "task_id": task["id"], "reused_result": True, "storage_status": "stored"}
    return None


def status(wallets, user, simulation_id):
    if result := stored_result(wallets, user, simulation_id):
        return result
    saved = path(wallets, simulation_id)
    if not saved.exists():
        raise ValueError("No saved CCIP simulation with this reference")
    result = wallets.vault.open(json.loads(saved.read_text())["encrypted"], f"ccip-simulation:{user}:{simulation_id}")
    return {**result, "reused_result": True}


def archive_result(wallets, user, result):
    """Import an existing local result into the ordinary task lifecycle without running it again."""
    store = wallets.store
    if store.backend != "arkiv":
        return result
    for task in store.tasks(user, protocol="chainlink_local"):
        progress = store.progress(user, task["id"])
        if progress["public_state"]["reference"] == result["id"]:
            return {**result, "task_id": task["id"], "storage_status": "stored"}

    def archived(task_id):
        storage_status = "stored" if task_id in store.receipts else "awaiting_storage_approval"
        return {**result, "task_id": task_id, "storage_status": storage_status}

    for row in store.db.execute("SELECT task_id,data FROM events WHERE user_id=? AND kind='tool_result'", (user,)):
        data = json.loads(row["data"])
        if data.get("tool") == "ccip_simulation_result" and data["output"]["id"] == result["id"]:
            store.flush_task(row["task_id"])
            return archived(row["task_id"])
    # The operation is already complete. Do not publish a runnable intermediate task.
    # This uses the normal storage wallet/approval path, not payment authorization.
    task = Store.create_task(
        store,
        user,
        "CCIP local simulation",
        "Recover the recorded local simulation result",
        "Verified local simulator result; no public payment claimed",
    )
    event_id = store.event(
        user,
        "tool_result",
        {"tool": "ccip_simulation_result", "ok": True, "evidence": True, "output": result},
        task_id=task["id"],
        source="tool",
    )
    Store.update_task(
        store,
        user,
        task["id"],
        status="completed",
        notes=f"Local simulation completed; evidence event {event_id}. No public-chain payment.",
    )
    store.flush_task(task["id"])
    return archived(task["id"])


async def simulate(wallets, user, recipient, amount):
    units = token_units(amount)
    if units > 10 * 10**18:
        raise ValueError("Local simulation accepts at most 10 CCIP-BnM")
    sender, recipient = address(wallets.get(user)["address"]), address(recipient)
    if int(recipient, 16) == 0:
        raise ValueError("Recipient must be a nonzero address")
    request = {"user": user, "sender": sender, "recipient": recipient, "units": str(units), "version": 1}
    simulation_id = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    async with wallets.lock:
        if path(wallets, simulation_id).exists():
            return archive_result(wallets, user, status(wallets, user, simulation_id))
        if saved := stored_result(wallets, user, simulation_id):
            return saved  # Losing a local result file does not authorize rerunning stored work.
        result = {
            "id": simulation_id,
            "kind": "ccip_simulation",
            "mode": "simulation",
            "status": "simulation_delivered",
            "asset": "CCIP-BnM",
            "amount": decimal(units),
            "sender": sender,
            "recipient": recipient,
            "scenario_source": "Ethereum Sepolia",
            "scenario_destination": "Base Sepolia",
            "execution_environment": "Chainlink Local Simulator in one isolated local EVM",
            "source_tx_hash": None,
            "public_delivery_verified": False,
            **await execute(wallets.config, sender, recipient, units, simulation_id),
        }
        Bridge.save_json(
            path(wallets, simulation_id),
            {"encrypted": wallets.vault.seal(result, f"ccip-simulation:{user}:{simulation_id}")},
        )
        return archive_result(wallets, user, result)


def review(result):
    return (
        "CCIP LOCAL SIMULATION — passed\n"
        f"Amount: {result['amount']} CCIP-BnM\n"
        f"Wallet address used in simulation: {result['sender']}\n"
        f"Recipient: {result['recipient']}\n"
        "Requested scenario: Ethereum Sepolia → Base Sepolia\n"
        "Execution: Chainlink Local Simulator in one local EVM; these are simulated balances.\n"
        f"Simulated native fee: {result['simulated_fee_eth']} ETH\n"
        f"Recipient token balance: {result['recipient_balance_before']} → {result['recipient_balance_after']}\n"
        f"Simulation message ID: {result['simulation_message_id']}\n"
        "Verified locally: delivery, recorded message ID, and rejection of a duplicate send.\n"
        "No payment signature or public-chain transaction was created. "
        "Your real ETH and CCIP-BnM balances are unchanged.\n"
        f"Simulation reference: {result['id']}"
        + (
            f"\nTask: {result['task_id']}. Arkiv storage: {result.get('storage_status', 'pending')}."
            if result.get("task_id")
            else ""
        )
        + ("\nReturning the saved result; the simulation was not run again." if result.get("reused_result") else "")
    )


def register_simulation_tools(registry):
    @registry.tool(modes=("chat",), evidence=True)
    async def simulate_ccip_transfer(ctx, recipient: str, amount: str):
        """Run the actual sender with Chainlink Local Simulator. No public transfer or wallet signature.
        Uses temporary faucet balances. Report simulation only; Sepolia/Base are scenario labels.
        """
        result = await simulate(ctx.engine.wallets, ctx.user_id, recipient, amount)
        ctx.payment_response = review(result)
        return result

    @registry.tool(modes=("chat",), evidence=True)
    async def ccip_simulation_status(ctx, simulation_id: str):
        """Read a saved local simulation result for this chat. This never proves public testnet delivery."""
        result = status(ctx.engine.wallets, ctx.user_id, simulation_id)
        ctx.payment_response = review(result)
        return result

    @registry.tool(modes=("chat",))
    async def archive_ccip_simulation(ctx, simulation_id: str):
        """Save an existing simulation result as a task checkpoint. Never reruns or sends a payment.
        Uses the normal Arkiv storage wallet and requires its approval when MetaMask is selected.
        """
        return archive_result(ctx.engine.wallets, ctx.user_id, status(ctx.engine.wallets, ctx.user_id, simulation_id))
