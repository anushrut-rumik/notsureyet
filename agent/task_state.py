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


def public_state(snapshot):
    task = snapshot["task"]
    state = dict.fromkeys(PUBLIC_FIELDS, "")
    state.update(status=task["status"], capability="task", protocol="ira", progress=task["status"])
    transfers = snapshot.get("transfers", [])
    if len(transfers) > 1:
        raise ValueError("A task cannot contain multiple payment intents")
    if transfers:
        payment = transfers[0]
        ccip = payment.get("kind") == "ccip"
        source = "arkiv_rabdi" if payment.get("kind") == "rabdi_native" else "ethereum_sepolia"
        state.update(
            capability="payment",
            protocol="ccip" if ccip else "native",
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
