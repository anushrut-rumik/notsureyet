"""Useful chat diagnostics without retaining provider bodies, credentials or prompts."""

import sqlite3

from agent.arkiv import ArkivError, ArkivSignatureRequired
from agent.inference import InferenceError


def record_chat_error(store, user_id, exc, *, phase="chat"):
    details = {"type": type(exc).__name__, "phase": phase}
    if isinstance(exc, InferenceError):
        details.update(exc.details())
    # Arbitrary exception text can contain a URL, credential or request body.
    # Known inference errors expose only our own safe codes and request identifiers.
    try:
        return store.event(user_id, "chat_error", details)
    except (sqlite3.Error, OSError):
        # Storage may be the original failure. Diagnostics must not replace it or kill the receiver.
        return None


def chat_error_message(exc):
    if isinstance(exc, (sqlite3.Error, OSError)):
        return (
            "Local storage is unavailable, so I couldn't finish this request. "
            "Check the existing task or receipt before retrying a payment."
        )
    if isinstance(exc, ArkivSignatureRequired):
        return str(exc)
    if isinstance(exc, InferenceError):
        return f"I couldn't get a response from the model. {exc} (Error: {exc.code}.)"
    if isinstance(exc, ArkivError):
        return (
            "I couldn't access task storage, so this step did not finish. "
            "I need the Arkiv connection restored before I can continue saved work."
        )
    return (
        "I couldn't finish this message because of an internal error. "
        "You can ask me to check the existing task's progress before retrying."
    )
