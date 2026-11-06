"""Explicit human approval replies, interpreted only against the latest exact review."""

import json
import re


def is_approval(text):
    return bool(
        re.fullmatch(
            r"/?(?:approve(?:\s+(?:it|this|this plan|the plan|the setup))?|approved|"
            r"i approve(?:\s+(?:it|this|the plan|the setup))?|yes(?:[, ]+please)?|"
            r"(?:yes[, ]+)?go ahead(?:\s+with (?:it|the plan|the setup))?|do it|looks good)[.!]?",
            text.strip(),
            re.I,
        )
    )


def approval_in_context(store, user, event_id, text):
    """Generic 'yes' needs the immediately preceding assistant turn to be a review."""
    if not is_approval(text):
        return False
    if re.match(r"/?(?:i\s+)?approve", text.strip(), re.I):
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
