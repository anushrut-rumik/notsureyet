"""Public, bounded task metadata. Never publish titles, instructions or approval codes."""

PUBLIC_FIELDS = (
    "status",
    "capability",
    "protocol",
    "source_network",
    "destination_network",
    "reference",
    "source_tx",
    "destination_tx",
    "order_id",
    "progress",
)
TERMINAL = {"completed", "cancelled"}


def public_state(snapshot, *, version=2):
    if version not in {1, 2}:
        raise ValueError("Unknown public task state version")
    task = snapshot["task"]
    state = dict.fromkeys(PUBLIC_FIELDS, "")
    state.update(status=task["status"], capability="task", protocol="ira", progress=task["status"])
    transfers = snapshot.get("transfers", [])
    if len(transfers) > 1:
        raise ValueError("A task cannot contain multiple payment intents")
    if transfers:
        payment = transfers[0]
        ccip = payment.get("kind") == "ccip"
        source = {7794095: "arkiv_rabdi", 84532: "base_sepolia"}.get(payment.get("chain_id"), "ethereum_sepolia")
        if version == 1:
            source = "arkiv_rabdi" if payment.get("kind") == "rabdi_native" else "ethereum_sepolia"
        state.update(
            capability="payment",
            protocol="ccip"
            if ccip
            else "erc20"
            if version == 2 and payment.get("kind") == "erc20_transfer"
            else "native",
            source_network=source,
            destination_network="base_sepolia" if ccip else source,
            reference=payment["id"],
            progress=payment["status"],
            source_tx=payment.get("source_tx_hash") or payment.get("tx_hash") or "",
            destination_tx=payment.get("destination_tx_hash") or "",
            order_id=payment.get("message_id") or payment.get("intent_id") or "",
        )
        if payment.get("kind") == "ccip_deployment":
            state.update(capability="deployment", protocol="ccip")
    else:
        tools = {event.get("data", {}).get("tool") for event in snapshot.get("events", [])}
        if "ccip_simulation_result" in tools:
            event = next(e for e in snapshot["events"] if e.get("data", {}).get("tool") == "ccip_simulation_result")
            result = event["data"]["output"]
            state.update(
                capability="simulation",
                protocol="chainlink_local",
                reference=result["id"],
                order_id=result["simulation_message_id"],
                progress=result["status"],
                source_network="local_evm",
                destination_network="local_evm",
            )
        elif "wait_until" in tools:
            state["capability"] = "reminder"
        elif tools & {"write_file", "read_file"}:
            state["capability"] = "file"
        elif tools & {"fetch_page", "search_web"}:
            state["capability"] = "research"
    if any(not isinstance(value, str) or len(value.encode()) > 128 for value in state.values()):
        raise ValueError("Task metadata must use bounded public identifiers")
    return state


def valid_stored_public_state(snapshot):
    """Unversioned historical snapshots used either deployed projection format.

    Both must still match the authenticated snapshot exactly. New snapshots name
    their format and cannot fall back to an earlier projection.
    """
    version = snapshot.get("public_state_version")
    if version is not None and (type(version) is not int or version not in {1, 2}):
        return False
    versions = (1, 2) if version is None else (version,)
    return any(snapshot.get("public_state") == public_state(snapshot, version=v) for v in versions)
