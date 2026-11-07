"""Structured channel facts. Approval stays in application code; models only write replies."""

from dataclasses import dataclass


@dataclass
class ChannelUpdate:
    facts: dict


def queue_update(store, user, event, facts, *, key=None):
    if key and store.db.execute(
        "SELECT 1 FROM events WHERE user_id=? AND kind='lending_update' "
        "AND json_extract(data,'$.key')=? LIMIT 1", (user, key),
    ).fetchone():
        return
    store.event(user, "lending_update", {"event": event, "facts": facts, "key": key})
