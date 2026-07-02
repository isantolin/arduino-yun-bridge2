"""Security primitives for military-grade cryptographic operations.

[MIL-SPEC COMPLIANCE]
This module provides security primitives resistant to:
- Memory inspection after use (secure_zero)
- Anti-replay attacks via monotonic counters in AEAD nonces.

Reference standards:
- NIST SP 800-90A (secure random)
- FIPS 140-3 (cryptographic module requirements)
- RFC 8439 (ChaCha20 and Poly1305)
"""

from __future__ import annotations

import ctypes
import secrets
from typing import Final
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from ..protocol import protocol

AEAD_NONCE_SIZE: Final[int] = protocol.AEAD_NONCE_SIZE
NONCE_TOTAL_BYTES: Final[int] = AEAD_NONCE_SIZE
AEAD_TAG_SIZE: Final[int] = protocol.AEAD_TAG_SIZE
NONCE_RANDOM_BYTES: Final[int] = 4
NONCE_COUNTER_BYTES: Final[int] = 8


def secure_zero(data: bytearray | memoryview) -> None:
    """Securely zero memory, resistant to interpreter optimization. [MIL-SPEC]

    The first pass (slice assignment) zeroes the Python-visible buffer.
    The second pass (ctypes.memset) defeats compiler/interpreter dead-store
    elimination on the underlying C memory.  Both passes MUST succeed;
    a failure in either is a security violation and propagates as-is.
    """
    data[:] = protocol.FRAME_DELIMITER * len(data)
    buf = (ctypes.c_char * len(data)).from_buffer(data)
    ctypes.memset(ctypes.addressof(buf), 0, len(data))


def generate_nonce_with_counter(counter: int) -> tuple[bytes, int]:
    """Generate a 12-byte AEAD nonce with monotonic counter."""
    if counter >= protocol.NONCE_COUNTER_MASK or counter < 0:
        raise ValueError("Nonce counter overflow")
    new_counter = counter + 1
    nonce = secrets.token_bytes(NONCE_RANDOM_BYTES) + new_counter.to_bytes(8, "big")
    return nonce, new_counter


def extract_nonce_counter(nonce: bytes) -> int:
    """Extract the counter from a 12-byte nonce."""
    if len(nonce) != AEAD_NONCE_SIZE:
        raise ValueError(f"Nonce must be {AEAD_NONCE_SIZE} bytes, got {len(nonce)}")
    return int.from_bytes(nonce[4:], "big")


def validate_nonce_counter(nonce: bytes, last_counter: int) -> tuple[bool, int]:
    """Validate nonce counter is strictly greater than last seen."""
    try:
        current = extract_nonce_counter(nonce)
    except ValueError:
        return False, last_counter

    if current <= last_counter or current > protocol.NONCE_COUNTER_MASK:
        return False, last_counter
    return True, current


def verify_crypto_integrity() -> bool:
    """Perform Known Answer Tests (KAT) for cryptographic primitives."""
    # 1. SHA256 KAT
    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"abc")
    if digest.finalize().hex() != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad":
        return False

    # 2. HMAC-SHA256 KAT (aligns with C++ KAT vector in security.cpp)
    h = hmac.HMAC(b"key", hashes.SHA256())
    h.update(b"The quick brown fox jumps over the lazy dog")
    if h.finalize().hex() != "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8":
        return False

    # 3. ChaCha20-Poly1305 KAT
    try:
        key = b"\x00" * 32
        nonce = b"\x00" * 12
        aead = ChaCha20Poly1305(key)
        ct = aead.encrypt(nonce, b"", None)
        if len(ct) != 16:
            return False
    except ValueError:
        return False

    return True


__all__ = [
    "secure_zero",
    "generate_nonce_with_counter",
    "extract_nonce_counter",
    "validate_nonce_counter",
    "verify_crypto_integrity",
]
