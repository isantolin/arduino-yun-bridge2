"""Security primitives for military-grade cryptographic operations.

[MIL-SPEC COMPLIANCE]
This module provides security primitives resistant to:
- Memory inspection after use (secure_zero)
- Timing side-channel attacks (timing_safe_equal - use hmac.compare_digest)

Reference standards:
- NIST SP 800-90A (secure random)
- FIPS 140-3 (cryptographic module requirements)
- CWE-14 (compiler removal of code to clear buffers)
"""

from __future__ import annotations

import ctypes
import hmac
import secrets
import struct
from typing import Final

# Constants for nonce format
NONCE_RANDOM_BYTES: Final[int] = 8
NONCE_COUNTER_BYTES: Final[int] = 8
NONCE_TOTAL_BYTES: Final[int] = NONCE_RANDOM_BYTES + NONCE_COUNTER_BYTES


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
    elif isinstance(data, bytearray):
        buf = (ctypes.c_char * len(data)).from_buffer(data)
        ctypes.memset(ctypes.addressof(buf), 0, len(data))
    else:
        raise TypeError(
            f"secure_zero requires bytearray or memoryview, got {type(data).__name__}"
        )


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


def timing_safe_equal(a: bytes, b: bytes) -> bool:
    """Timing-safe comparison of two byte strings.

    [MIL-SPEC] Wrapper around hmac.compare_digest for clarity.
    Compares in constant time regardless of where first difference occurs.

    Use this for comparing:
    - HMAC tags
    - Authentication tokens
    - Password hashes
    - Any security-sensitive comparison

    Args:
        a: First bytes object.
        b: Second bytes object.

    Returns:
        True if equal, False otherwise.

    Reference: CWE-208 (Observable Timing Discrepancy)
    """
    return hmac.compare_digest(a, b)


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
    counter_part = struct.pack(">Q", new_counter)  # Big-endian uint64
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
    return struct.unpack(">Q", nonce[NONCE_RANDOM_BYTES:])[0]


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


__all__ = [
    "NONCE_COUNTER_BYTES",
    "NONCE_RANDOM_BYTES",
    "NONCE_TOTAL_BYTES",
    "extract_nonce_counter",
    "generate_nonce_with_counter",
    "secure_zero",
    "secure_zero_bytes_copy",
    "timing_safe_equal",
    "validate_nonce_counter",
]
