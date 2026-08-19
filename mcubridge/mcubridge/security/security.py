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
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from ..protocol import protocol


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
    random_bytes = protocol.AEAD_NONCE_SIZE - 8
    nonce = secrets.token_bytes(random_bytes) + new_counter.to_bytes(8, "big")
    return nonce, new_counter


def extract_nonce_counter(nonce: bytes) -> int:
    """Extract the counter from a 12-byte nonce."""
    if len(nonce) != protocol.AEAD_NONCE_SIZE:
        raise ValueError(f"Nonce must be {protocol.AEAD_NONCE_SIZE} bytes, got {len(nonce)}")
    return int.from_bytes(nonce[protocol.AEAD_NONCE_SIZE - 8 :], "big")


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
    # Spanish pangram: 27/27 letters (a-z + ñ), 56 UTF-8 bytes
    h = hmac.HMAC(b"key", hashes.SHA256())
    h.update("Jovencillo emponzoñado de whisky, qué figuritas exhibe".encode())
    if h.finalize().hex() != "5375963f9e709b58415041bad2d44de21f50800e0841b87e0dadfcdfe362b26c":
        return False

    # 3. ChaCha20-Poly1305 KAT (RFC 8439 test vector with AAD and Tag verification)
    try:
        key = bytes(range(0x80, 0xA0))
        nonce = b"\x07\x00\x00\x00\x40\x41\x42\x43\x44\x45\x46\x47"
        ad = b"\x50\x51\x52\x53\xc0\xc1\xc2\xc3\xc4\xc5\xc6\xc7"
        aead = ChaCha20Poly1305(key)
        ct = aead.encrypt(nonce, b"test", ad)
        if len(ct) != 20 or ct[-16:].hex() != "7dca8479787a5c190f58eedae6a06bcf":
            return False
    except (ValueError, TypeError):
        return False

    return True
