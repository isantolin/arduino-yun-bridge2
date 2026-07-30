"""Exhaustive tests for mcubridge.security.security module. [SIL-2]"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from mcubridge.protocol import protocol
from mcubridge.security.security import (
    extract_nonce_counter,
    generate_nonce_with_counter,
    secure_zero,
    validate_nonce_counter,
    verify_crypto_integrity,
)


def test_secure_zero_bytearray() -> None:
    data = bytearray(b"sensitive_password_123")
    secure_zero(data)
    assert data == bytearray(len(data))


def test_secure_zero_memoryview() -> None:
    raw = bytearray(b"top_secret_data")
    view = memoryview(raw)
    secure_zero(view)
    assert raw == bytearray(len(raw))


def test_generate_nonce_with_counter_success() -> None:
    nonce, new_counter = generate_nonce_with_counter(0)
    assert len(nonce) == 12
    assert new_counter == 1
    assert extract_nonce_counter(nonce) == 1


def test_generate_nonce_with_counter_overflow() -> None:
    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(protocol.NONCE_COUNTER_MASK)

    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(-1)


def test_extract_nonce_counter_invalid_length() -> None:
    with pytest.raises(ValueError, match="Nonce must be 12 bytes"):
        extract_nonce_counter(b"short")


def test_validate_nonce_counter_valid() -> None:
    nonce, _ = generate_nonce_with_counter(10)
    valid, new_last = validate_nonce_counter(nonce, 10)
    assert valid is True
    assert new_last == 11


def test_validate_nonce_counter_invalid_length() -> None:
    valid, last = validate_nonce_counter(b"invalid_len", 5)
    assert valid is False
    assert last == 5


def test_validate_nonce_counter_replay() -> None:
    nonce, _ = generate_nonce_with_counter(5)  # counter = 6
    valid, last = validate_nonce_counter(nonce, 6)  # current == last -> invalid
    assert valid is False
    assert last == 6


def test_validate_nonce_counter_overflow_mask() -> None:
    with patch("mcubridge.security.security.extract_nonce_counter", return_value=protocol.NONCE_COUNTER_MASK + 1):
        valid, last = validate_nonce_counter(b"\x00" * 12, 10)
        assert valid is False
        assert last == 10


def test_verify_crypto_integrity_success() -> None:
    assert verify_crypto_integrity() is True


def test_verify_crypto_integrity_sha256_failure() -> None:
    with patch("cryptography.hazmat.primitives.hashes.Hash.finalize", return_value=b"wrong_hash"):
        assert verify_crypto_integrity() is False


def test_verify_crypto_integrity_hmac_failure() -> None:
    with patch("cryptography.hazmat.primitives.hmac.HMAC.finalize", return_value=b"wrong_hmac"):
        assert verify_crypto_integrity() is False


def test_verify_crypto_integrity_chacha_failure() -> None:
    with patch(
        "cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt",
        side_effect=ValueError("ChaCha error"),
    ):
        assert verify_crypto_integrity() is False
