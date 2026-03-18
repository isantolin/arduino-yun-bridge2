"""Minimal cryptography.hazmat.primitives.hmac stub for local tests."""
import hmac as std_hmac
import hashlib
from typing import Any

class HMAC:
    def __init__(self, key: bytes, algorithm: Any, backend: Any = None) -> None:
        self._h = std_hmac.new(key, msg=None, digestmod=hashlib.sha256)

    def update(self, data: bytes) -> None:
        self._h.update(data)

    def finalize(self) -> bytes:
        return self._h.digest()

    def copy(self) -> "HMAC":
        # Simplified copy for stub
        return self

    def verify(self, signature: bytes) -> None:
        if not std_hmac.compare_digest(self.finalize(), signature):
            raise ValueError("Invalid signature")
