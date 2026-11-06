"""Explicit human approval replies, interpreted only against the latest exact review."""

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
