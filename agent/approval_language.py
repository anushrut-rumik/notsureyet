"""Explicit human approval replies, interpreted only against the latest exact review."""

import json
import re


def normalize_approval(text):
    """Remove only conversational padding; preserve negation and changes of scope."""
    text = " ".join(text.lower().replace("’", "'").strip().lstrip("/").split())
    text = text.rstrip(".!")
    # Full clauses, not substring matching: 'go ahead but change the wallet'
    # must never become an approval of the original wallet.
    text = re.sub(r"^(?:(?:ok(?:ay)?|cool|alright|please)[,!. ]+)+", "", text)
    text = re.sub(r"(?:[,!. ]+(?:please|now|let(?:'s|s)? (?:go|do (?:this|it))))+$", "", text)
    return text.rstrip(".!")


def is_approval(text):
    # People combine affirmative clauses: "yes do this approved". Match the
    # whole message, not the occurrence of "approved" inside a condition,
    # question, negation, or requested change to the reviewed action.
    clause = (
        r"(?:please\s+)?(?:"
        r"(?:i\s+)?approve(?:\s+(?:it|this|this plan|the plan|the setup))?|approved|"
        r"yes(?:[, ]+please)?|go ahead(?:\s+with (?:it|the plan|the setup))?|"
        r"do (?:it|this)|looks good)"
    )
    return bool(re.fullmatch(rf"{clause}(?:[,!.\s]+{clause})*", normalize_approval(text), re.I))


def approval_in_context(store, user, event_id, text):
    """Generic 'yes' needs the immediately preceding assistant turn to be a review."""
    if not is_approval(text):
        return False
    if re.search(r"\bapprove(?:d)?\b", normalize_approval(text), re.I):
        return True
    rows = store.db.execute(
        "SELECT data FROM events WHERE user_id=? AND id<? AND kind='chat_message' AND source!='user' ORDER BY id DESC",
        (user, event_id),
    )
    for row in rows:
        message = json.loads(row[0])
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        return bool(
            "Setup reference:" in content
            or "Plan reference:" in content
            or re.search(r"\bapprove (?:lending |[a-f0-9]{12}\b)", content, re.I)
            or content.startswith(("Setup approved.", "That setup is already approved."))
        )
    return False
