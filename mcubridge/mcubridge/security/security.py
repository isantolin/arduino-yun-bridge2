"""Cryptographic helpers for the MCU Bridge protocol.

The implementation uses RFC 8439 AEAD and project-defined KATs. These helpers
do not imply FIPS 140-3 validation or process-wide secret zeroization.
"""

from __future__ import annotations

import ctypes
import os

import structlog
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from ..protocol import protocol

os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

logger = structlog.get_logger("mcubridge.security")


def secure_zero(data: bytearray | memoryview) -> None:
    """Overwrite this mutable buffer in place; other copies are not covered."""
    data[:] = protocol.FRAME_DELIMITER * len(data)
    buf = (ctypes.c_char * len(data)).from_buffer(data)
    ctypes.memset(ctypes.addressof(buf), 0, len(data))


def generate_nonce_with_counter(counter: int) -> tuple[bytes, int]:
    """Generate an MPU-originated AEAD nonce with a directional prefix and counter."""
    if counter >= protocol.NONCE_COUNTER_MASK or counter < 0:
        raise ValueError("Nonce counter overflow")
    new_counter = counter + 1
    prefix_bytes = protocol.AEAD_NONCE_SIZE - protocol.HANDSHAKE_NONCE_COUNTER_BYTES
    prefix = protocol.MPU_AEAD_NONCE_PREFIX.to_bytes(prefix_bytes, "big")
    nonce = prefix + new_counter.to_bytes(protocol.HANDSHAKE_NONCE_COUNTER_BYTES, "big")
    return nonce, new_counter


def extract_nonce_counter(
    nonce: bytes,
    expected_prefix: int | None = protocol.MCU_AEAD_NONCE_PREFIX,
) -> int:
    """Extract the counter from a 12-byte nonce, validating directional prefix."""
    if len(nonce) != protocol.AEAD_NONCE_SIZE:
        raise ValueError(f"Nonce must be {protocol.AEAD_NONCE_SIZE} bytes, got {len(nonce)}")
    counter_offset = protocol.AEAD_NONCE_SIZE - protocol.HANDSHAKE_NONCE_COUNTER_BYTES
    if expected_prefix is not None:
        prefix_val = int.from_bytes(nonce[:counter_offset], "big")
        if prefix_val != expected_prefix:
            raise ValueError(f"Nonce prefix mismatch: expected {expected_prefix:#010x}, got {prefix_val:#010x}")
    return int.from_bytes(nonce[counter_offset:], "big")


def validate_nonce_counter(
    nonce: bytes,
    last_counter: int,
    expected_prefix: int = protocol.MCU_AEAD_NONCE_PREFIX,
) -> tuple[bool, int]:
    """Validate nonce counter is strictly greater than last seen and prefix matches expected source."""
    try:
        current = extract_nonce_counter(nonce, expected_prefix=expected_prefix)
    except ValueError as exc:
        logger.warning("Failed to extract nonce counter", error=str(exc))
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
        tag_size = protocol.AEAD_TAG_SIZE
        if len(ct) != 20 or ct[-tag_size:].hex() != "7dca8479787a5c190f58eedae6a06bcf":
            return False
    except (ValueError, TypeError) as exc:
        logger.error("Cryptographic Known Answer Test (KAT) failed", error=str(exc))
        return False

    return True
