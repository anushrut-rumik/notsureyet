"""Safe inference errors, without logging request bodies or keys."""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from openai import APIConnectionError


class InferenceError(RuntimeError):
    def __init__(self, code, message, *, retryable=False, retry_after=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.request_id = request_id
        self.client_request_id = None

    def details(self):
        return {
            "code": self.code,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "request_id": self.request_id,
            "client_request_id": self.client_request_id,
        }


def retry_delay(value):
    """Retry-After can be seconds or an HTTP date; never shorten a server's delay."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def safe_id(value):
    # Codes/IDs are useful for support; arbitrary server text may echo a prompt or credential.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value) else None


def inference_error(exc):
    if isinstance(exc, APIConnectionError):
        return InferenceError("connection_error", "Could not reach inference; check the connection.", retryable=True)
    body = exc.body if isinstance(exc.body, dict) else {}
    try:
        envelope = exc.response.json()
    except ValueError:
        envelope = {}
    if not isinstance(envelope, dict):
        envelope = {}
    error = body.get("error", body)
    code = safe_id(error.get("code")) if isinstance(error, dict) else None
    status = exc.status_code
    message = {
        400: "Inference request or model settings are invalid; check the catalog and configuration.",
        401: "The inference key is invalid or revoked; replace it in .env.",
        402: "Inference billing is unavailable; check the provider account's billing settings.",
        403: "The inference key does not permit this request; check its scope and model access.",
        404: "The inference model or endpoint was not found; check the configuration.",
        429: "Inference is rate limited; wait for the provider's retry interval.",
    }.get(status, "The inference provider could not complete the request.")
    if code == "insufficient_quota":
        message = "Inference quota is exhausted; check the provider account's billing and usage limits."
    return InferenceError(
        code or f"http_{status}",
        message,
        retryable=(status in {408, 409, 429} or status >= 500) and code != "insufficient_quota",
        retry_after=retry_delay(exc.response.headers.get("retry-after")),
        request_id=safe_id(envelope.get("request_id")) or safe_id(body.get("request_id")) or safe_id(exc.request_id),
    )
