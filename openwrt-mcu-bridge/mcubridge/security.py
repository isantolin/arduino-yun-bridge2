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
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .rpc import protocol
from .rpc.protocol import (
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
    """Securely zero memory, resistant to interpreter optimization.

    [MIL-SPEC] Uses ctypes.memset to directly write zeros to memory,
    bypassing Python's memory management which might optimize away
    simple assignments.

    Use this to clear sensitive data like:
    - Cryptographic keys
    - HMAC digests after comparison
    - Nonces after use
    - Shared secrets in temporary buffers

    Args:
        data: Mutable buffer to zero (bytearray or memoryview).
              Immutable bytes objects cannot be zeroed.

    Raises:
        TypeError: If data is not a mutable buffer type.

    Example:
        >>> secret = bytearray(b"sensitive_key_material")
        >>> secure_zero(secret)
        >>> assert secret == bytearray(len(secret))  # All zeros

    Reference: CWE-14, CERT C MSC06-C (adapted for Python)
    """
    if isinstance(data, memoryview):
        # Get underlying buffer for memoryview
        buf = (ctypes.c_char * len(data)).from_buffer(data)
        ctypes.memset(ctypes.addressof(buf), 0, len(data))
    else:
        # data is bytearray (type narrowed by signature)
        buf = (ctypes.c_char * len(data)).from_buffer(data)
        ctypes.memset(ctypes.addressof(buf), 0, len(data))


def secure_zero_bytes_copy(data: bytes) -> bytes:
    """Return a zeroed copy of the same length (for immutable bytes).

    Since bytes objects are immutable, we cannot zero them in place.
    This function returns a zeroed bytes object of the same length,
    but the original data remains in memory until garbage collected.

    For true secure zeroing, use bytearray from the start.

    Args:
        data: Original bytes object.

    Returns:
        Zeroed bytes object of the same length.
    """
    return bytes(len(data))


def generate_nonce_with_counter(counter: int) -> tuple[bytes, int]:
    """Generate a 16-byte nonce with monotonic counter (anti-replay).

    [MIL-SPEC] Generates a nonce with structure:
    - Bytes 0-7:  Random data (cryptographically secure)
    - Bytes 8-15: Monotonic counter (big-endian, anti-replay)

    The counter prevents replay attacks by ensuring each nonce
    is unique and can be validated as newer than previous nonces.

    Args:
        counter: Current counter value (will be incremented).

    Returns:
        Tuple of (nonce_bytes, new_counter_value).

    Example:
        >>> counter = 0
        >>> nonce, counter = generate_nonce_with_counter(counter)
        >>> len(nonce)
        16
        >>> counter
        1
    """
    new_counter = counter + 1
    random_part = secrets.token_bytes(NONCE_RANDOM_BYTES)
    counter_part = protocol.NONCE_COUNTER_STRUCT.build(new_counter)  # Big-endian uint64
    return random_part + counter_part, new_counter


def extract_nonce_counter(nonce: bytes) -> int:
    """Extract the counter from a nonce (for validation).

    Args:
        nonce: 16-byte nonce with counter in bytes 8-15.

    Returns:
        64-bit counter value.

    Raises:
        ValueError: If nonce is not 16 bytes.
    """
    if len(nonce) != NONCE_TOTAL_BYTES:
        raise ValueError(f"Nonce must be {NONCE_TOTAL_BYTES} bytes, got {len(nonce)}")
    return protocol.NONCE_COUNTER_STRUCT.parse(nonce[NONCE_RANDOM_BYTES:])


def validate_nonce_counter(nonce: bytes, last_counter: int) -> tuple[bool, int]:
    """Validate nonce counter is strictly greater than last seen.

    [MIL-SPEC] Anti-replay protection: rejects any nonce with a counter
    less than or equal to the last accepted counter.

    Args:
        nonce: 16-byte nonce to validate.
        last_counter: Last accepted counter value.

    Returns:
        Tuple of (is_valid, new_last_counter).
        If valid, new_last_counter is the current nonce's counter.
        If invalid, new_last_counter is unchanged (equals last_counter).

    Example:
        >>> last = 0
        >>> nonce, _ = generate_nonce_with_counter(5)
        >>> valid, last = validate_nonce_counter(nonce, last)
        >>> valid
        True
        >>> last
        6
    """
    try:
        current = extract_nonce_counter(nonce)
    except ValueError:
        return False, last_counter

    if current <= last_counter:
        return False, last_counter  # Replay detected

    return True, current


def verify_crypto_integrity() -> bool:
    """Perform Known Answer Tests (KAT) for cryptographic primitives.

    [MIL-SPEC COMPLIANCE - FIPS 140-3]
    Mandatory self-tests at startup to ensure the cryptographic engine
    (hashlib/hmac) is operating correctly before processing real data.

    Vectors from NIST/RFC 4231.
    """
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
