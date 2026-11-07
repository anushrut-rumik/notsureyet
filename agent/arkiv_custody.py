"""Single-host Rabdi signer coordination. CRE's other chains never take this lock."""

import fcntl
import json
from contextlib import contextmanager


@contextmanager
def signer_lock(config, writer, *, wait=False):
    path = config.data_dir / f"arkiv-signer-{writer.lower()}.lock"
    with path.open("a+") as file:
        path.chmod(0o600)
        try:
            fcntl.flock(file, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError("Rabdi storage signer is busy; retry after the current storage operation.") from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def activity_reserved(config, writer):
    path = config.data_dir / "lending-activity" / f"reservation-{writer.lower()}.json"
    return path.exists() and json.loads(path.read_text()).get("state") != "verified"
