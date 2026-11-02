"""Cryptographic primitives and link-frame protection for the MCU transport."""

from __future__ import annotations

import os

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..protocol import protocol

os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

logger = structlog.get_logger("mcubridge.security")


def derive_link_keys(secret: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    """Derive host-to-MCU and MCU-to-host keys from the shared secret."""
    if not secret:
        raise ValueError("link shared secret is empty")
    if len(nonce) != protocol.HANDSHAKE_NONCE_LENGTH:
        raise ValueError("link handshake nonce has an invalid length")
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=2 * protocol.AEAD_KEY_SIZE,
        salt=nonce,
        info=protocol.HANDSHAKE_HKDF_INFO_SESSION,
    ).derive(secret)
    return (
        key_material[: protocol.AEAD_KEY_SIZE],
        key_material[protocol.AEAD_KEY_SIZE :],
    )


class LinkCipher:
    """Directional ChaCha20-Poly1305 state for one authenticated serial session."""

    __slots__ = ("_tx", "_rx", "_tx_counter", "_rx_counter")

    def __init__(self, tx_key: bytes, rx_key: bytes) -> None:
        if len(tx_key) != protocol.AEAD_KEY_SIZE or len(rx_key) != protocol.AEAD_KEY_SIZE:
            raise ValueError("link AEAD keys have an invalid length")
        self._tx = ChaCha20Poly1305(tx_key)
        self._rx = ChaCha20Poly1305(rx_key)
        self._tx_counter = 0
        self._rx_counter = 0

    @classmethod
    def for_host(cls, secret: bytes, nonce: bytes) -> LinkCipher:
        host_to_mcu_key, mcu_to_host_key = derive_link_keys(secret, nonce)
        return cls(host_to_mcu_key, mcu_to_host_key)

    def encrypt(self, address: int, plaintext: bytes) -> bytes:
        """Return counter || ciphertext || tag for one HDLC channel payload."""
        self._validate_address(address)
        if self._tx_counter >= 0xFFFFFFFFFFFFFFFF:
            raise OverflowError("link AEAD transmit counter exhausted")

        self._tx_counter += 1
        counter = self._tx_counter.to_bytes(protocol.AEAD_COUNTER_SIZE, "big")
        nonce = bytes(protocol.AEAD_NONCE_SIZE - protocol.AEAD_COUNTER_SIZE) + counter
        aad = bytes((address,)) + counter
        ciphertext_and_tag = self._tx.encrypt(nonce, plaintext, aad)
        return counter + ciphertext_and_tag

    def decrypt(self, address: int, envelope: bytes | memoryview) -> bytes:
        """Authenticate and decrypt a frame, rejecting replayed counters."""
        self._validate_address(address)
        minimum_size = protocol.AEAD_COUNTER_SIZE + protocol.AEAD_TAG_SIZE
        if len(envelope) < minimum_size:
            raise ValueError("link AEAD frame is truncated")

        counter_bytes = bytes(envelope[: protocol.AEAD_COUNTER_SIZE])
        counter = int.from_bytes(counter_bytes, "big")
        if counter == 0 or counter <= self._rx_counter:
            raise ValueError("link AEAD frame counter is invalid or replayed")

        nonce = bytes(protocol.AEAD_NONCE_SIZE - protocol.AEAD_COUNTER_SIZE) + counter_bytes
        aad = bytes((address,)) + counter_bytes
        try:
            plaintext = self._rx.decrypt(
                nonce,
                bytes(envelope[protocol.AEAD_COUNTER_SIZE :]),
                aad,
            )
        except InvalidTag as exc:
            raise ValueError("link AEAD frame authentication failed") from exc

        self._rx_counter = counter
        return plaintext

    @staticmethod
    def _validate_address(address: int) -> None:
        if not 0 <= address <= 0xFF:
            raise ValueError("HDLC address is out of range")


def verify_crypto_integrity() -> bool:
    """Run known-answer checks for SHA-256, HMAC-SHA256, and ChaCha20-Poly1305."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"abc")
    if digest.finalize().hex() != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad":
        return False

    h = hmac.HMAC(b"key", hashes.SHA256())
    h.update("Jovencillo emponzoñado de whisky, qué figuritas exhibe".encode())
    if h.finalize().hex() != "5375963f9e709b58415041bad2d44de21f50800e0841b87e0dadfcdfe362b26c":
        return False

    try:
        key = bytes(range(0x80, 0xA0))
        nonce = b"\x07\x00\x00\x00\x40\x41\x42\x43\x44\x45\x46\x47"
        aad = b"\x50\x51\x52\x53\xc0\xc1\xc2\xc3\xc4\xc5\xc6\xc7"
        aead = ChaCha20Poly1305(key)
        ciphertext_and_tag = aead.encrypt(nonce, b"test", aad)
        expected = bytes.fromhex("eb1e9a297dca8479787a5c190f58eedae6a06bcf")
        if ciphertext_and_tag != expected:
            return False
        return aead.decrypt(nonce, ciphertext_and_tag, aad) == b"test"
    except (InvalidTag, ValueError, TypeError) as exc:
        logger.error("Cryptographic known-answer test failed", error=str(exc))
        return False
