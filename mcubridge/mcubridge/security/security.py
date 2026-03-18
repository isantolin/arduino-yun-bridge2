"""Security primitives for military-grade cryptographic operations.

[MIL-SPEC COMPLIANCE]
This module provides security primitives resistant to:
- Memory inspection after use (secure_zero)

Reference standards:
- NIST SP 800-90A (secure random)
- FIPS 140-3 (cryptographic module requirements)
- CWE-14 (compiler removal of code to clear buffers)
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import secrets
import struct
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..protocol.protocol import (
    HANDSHAKE_HKDF_INFO_AUTH,
    HANDSHAKE_HKDF_SALT,
)

# Constants for nonce format
NONCE_RANDOM_BYTES: Final[int] = 8
NONCE_COUNTER_BYTES: Final[int] = 8
NONCE_TOTAL_BYTES: Final[int] = NONCE_RANDOM_BYTES + NONCE_COUNTER_BYTES


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """Derive a key using HKDF-SHA256 via native cryptography library."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
    )
    return hkdf.derive(ikm)


def derive_handshake_key(shared_secret: bytes) -> bytes:
    """Derive the internal handshake authentication key."""
    return hkdf_sha256(
        shared_secret,
        HANDSHAKE_HKDF_SALT,
        HANDSHAKE_HKDF_INFO_AUTH,
        32,
    )


def secure_zero(data: bytearray | memoryview) -> None:
    """Securely zero memory, resistant to interpreter optimization."""
    buf = (ctypes.c_char * len(data)).from_buffer(data)
    ctypes.memset(ctypes.addressof(buf), 0, len(data))


def secure_zero_bytes_copy(data: bytes) -> bytes:
    """Return a zeroed copy of the same length (for immutable bytes)."""
    return bytes(len(data))


def generate_nonce_with_counter(counter: int) -> tuple[bytes, int]:
    """Generate a 16-byte nonce with monotonic counter using struct.pack_into."""
    new_counter = counter + 1
    nonce = bytearray(NONCE_TOTAL_BYTES)
    nonce[:NONCE_RANDOM_BYTES] = secrets.token_bytes(NONCE_RANDOM_BYTES)
    struct.pack_into(">Q", nonce, NONCE_RANDOM_BYTES, new_counter)
    return bytes(nonce), new_counter


def extract_nonce_counter(nonce: bytes) -> int:
    """Extract the counter from a nonce using struct.unpack_from."""
    if len(nonce) != NONCE_TOTAL_BYTES:
        raise ValueError(f"Nonce must be {NONCE_TOTAL_BYTES} bytes, got {len(nonce)}")
    (val,) = struct.unpack_from(">Q", nonce, NONCE_RANDOM_BYTES)
    return val


def validate_nonce_counter(nonce: bytes, last_counter: int) -> tuple[bool, int]:
    """Validate nonce counter is strictly greater than last seen."""
    try:
        current = extract_nonce_counter(nonce)
    except ValueError:
        return False, last_counter

    if current <= last_counter:
        return False, last_counter  # Replay detected

    return True, current


def verify_crypto_integrity() -> bool:
    """Perform Known Answer Tests (KAT) for cryptographic primitives."""
    # 1. SHA256 KAT ("abc")
    msg = b"abc"
    expected_sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    actual_sha = hashlib.sha256(msg).hexdigest()
    if actual_sha != expected_sha:
        return False

    # 2. HMAC-SHA256 KAT
    key = b"key"
    data = b"The quick brown fox jumps over the lazy dog"
    expected_hmac = "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    actual_hmac = hmac.new(key, data, hashlib.sha256).hexdigest()
    if actual_hmac != expected_hmac:
        return False

    return True


__all__ = [
    "NONCE_COUNTER_BYTES",
    "NONCE_RANDOM_BYTES",
    "NONCE_TOTAL_BYTES",
    "extract_nonce_counter",
    "generate_nonce_with_counter",
    "secure_zero",
    "secure_zero_bytes_copy",
    "validate_nonce_counter",
    "verify_crypto_integrity",
    "hkdf_sha256",
    "derive_handshake_key",
]
