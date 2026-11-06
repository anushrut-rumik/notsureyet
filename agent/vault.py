"""Independent local encryption key. Back it up; it is required for recovery."""

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Vault:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "vault.key"
        if not self.path.exists():
            if (directory / "arkiv-writer.json").exists() or any(directory.glob("wallet-*.json")):
                raise ValueError("vault.key is missing. Restore its backup before opening this workspace.")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(AESGCM.generate_key(bit_length=256))
                file.flush()
                os.fsync(file.fileno())
        self.key = self.path.read_bytes()
        self.aes = AESGCM(self.key)

    def seal(self, value, context: str) -> str:
        nonce = os.urandom(12)
        data = json.dumps(value, separators=(",", ":")).encode()
        return base64.b64encode(nonce + self.aes.encrypt(nonce, data, context.encode())).decode()

    def open(self, value: str, context: str):
        try:
            data = base64.b64decode(value, validate=True)
            return json.loads(self.aes.decrypt(data[:12], data[12:], context.encode()))
        except Exception:
            raise ValueError("Cannot decrypt checkpoint. Restore the original vault.key; do not replace it.") from None
