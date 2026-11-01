"""Cryptographic helpers for the MCU Bridge protocol.

The implementation uses RFC 8439 AEAD and project-defined KATs. These helpers
do not imply FIPS 140-3 validation or process-wide secret zeroization.
"""

from __future__ import annotations

import os

import structlog
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from ..protocol import protocol

os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

logger = structlog.get_logger("mcubridge.security")


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
