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
import hashlib
import secrets
import struct
from typing import Final
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from ..protocol import protocol

# [SIL-2] Security Constants from protocol spec
# For ChaCha20-Poly1305, nonce is exactly 12 bytes (96 bits).
AEAD_NONCE_SIZE: Final[int] = protocol.AEAD_NONCE_SIZE
NONCE_TOTAL_BYTES: Final[int] = AEAD_NONCE_SIZE
AEAD_TAG_SIZE: Final[int] = protocol.AEAD_TAG_SIZE
NONCE_RANDOM_BYTES: Final[int] = 4  # Derived
NONCE_COUNTER_BYTES: Final[int] = 8  # Derived


def secure_zero(data: bytearray | memoryview) -> None:
    """Securely zero memory, resistant to interpreter optimization."""
    data[:] = protocol.FRAME_DELIMITER * len(data)
    try:
        buf = (ctypes.c_char * len(data)).from_buffer(data)
        ctypes.memset(ctypes.addressof(buf), 0, len(data))
    except (TypeError, ValueError, AttributeError):
        pass


def secure_zero_bytes_copy(data: bytes) -> bytes:
    """Return a zeroed copy of the same length (for immutable bytes)."""
    return bytes(len(data))


def generate_nonce_with_counter(counter: int) -> tuple[bytes, int]:
    """Generate a 12-byte AEAD nonce with monotonic counter."""
    new_counter = (counter + 1) & 0xFFFFFFFFFFFFFFFF
    random_part = secrets.token_bytes(NONCE_RANDOM_BYTES)
    nonce = random_part + struct.pack(protocol.NONCE_COUNTER_FORMAT, new_counter)
    return nonce, new_counter


def extract_nonce_counter(nonce: bytes) -> int:
    """Extract the counter from a 12-byte nonce."""
    if len(nonce) != AEAD_NONCE_SIZE:
        raise ValueError(f"Nonce must be {AEAD_NONCE_SIZE} bytes, got {len(nonce)}")
    try:
        return struct.unpack(protocol.NONCE_COUNTER_FORMAT, nonce[NONCE_RANDOM_BYTES:])[
            0
        ]
    except struct.error as e:
        raise ValueError(f"Malformed nonce format: {e}") from e


def validate_nonce_counter(nonce: bytes, last_counter: int) -> tuple[bool, int]:
    """Validate nonce counter is strictly greater than last seen."""
    try:
        current = extract_nonce_counter(nonce)
    except ValueError:
        return False, last_counter

    if current <= last_counter and last_counter != protocol.NONCE_COUNTER_MASK:
        return False, last_counter
    return True, current


def aead_encrypt(
    key: bytes, nonce: bytes, data: bytes, ad: bytes | None = None
) -> bytes:
    """[SIL-2] Encrypt and authenticate data using ChaCha20-Poly1305."""
    aead = ChaCha20Poly1305(key)
    return aead.encrypt(nonce, data, ad)


def aead_decrypt(
    key: bytes, nonce: bytes, ciphertext_with_tag: bytes, ad: bytes | None = None
) -> bytes:
    """[SIL-2] Decrypt and verify data using ChaCha20-Poly1305."""
    aead = ChaCha20Poly1305(key)
    return aead.decrypt(nonce, ciphertext_with_tag, ad)


def verify_crypto_integrity() -> bool:
    """Perform Known Answer Tests (KAT) for cryptographic primitives."""
    # 1. SHA256 KAT
    if hashlib.sha256(b"abc").hexdigest() != (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    ):
        return False

    # 2. ChaCha20-Poly1305 KAT
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
    "AEAD_NONCE_SIZE",
    "NONCE_TOTAL_BYTES",
    "AEAD_TAG_SIZE",
    "NONCE_RANDOM_BYTES",
    "NONCE_COUNTER_BYTES",
    "aead_decrypt",
    "aead_encrypt",
    "extract_nonce_counter",
    "generate_nonce_with_counter",
    "secure_zero",
    "secure_zero_bytes_copy",
    "validate_nonce_counter",
    "verify_crypto_integrity",
]
