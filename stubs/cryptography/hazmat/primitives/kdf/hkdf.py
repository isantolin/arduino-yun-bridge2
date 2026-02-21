"""Minimal HKDF-SHA256 implementation for tests."""

from __future__ import annotations

import hashlib
import hmac
from math import ceil
from typing import Any


class HKDF:
    def __init__(
        self,
        *,
        algorithm: Any,
        length: int,
        salt: bytes | None,
        info: bytes | None,
    ) -> None:
        self._algorithm = algorithm
        self._length = int(length)
        self._salt = salt or b""
        self._info = info or b""

    def derive(self, ikm: bytes) -> bytes:
        if self._length <= 0:
            return b""
        if getattr(self._algorithm, "name", "").lower() != "sha256":
            raise ValueError("Only SHA256 is supported by this test stub")

        hash_len = 32
        salt = self._salt if self._salt else b"\x00" * hash_len
        prk = hmac.new(salt, ikm, hashlib.sha256).digest()

        blocks = []
        previous = b""
        n = ceil(self._length / hash_len)
        for index in range(1, n + 1):
            previous = hmac.new(
                prk,
                previous + self._info + bytes([index]),
                hashlib.sha256,
            ).digest()
            blocks.append(previous)
        return b"".join(blocks)[: self._length]

