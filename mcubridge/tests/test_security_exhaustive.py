"""Exhaustive tests for mcubridge.security.security module. [SIL-2]"""

from __future__ import annotations

import pytest
from hypothesis import example, given, strategies as st
from pytest_mock import MockerFixture

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


@given(counter=st.integers(min_value=0, max_value=protocol.NONCE_COUNTER_MASK - 1))
@example(counter=0)
@example(counter=protocol.NONCE_COUNTER_MASK - 2)
def test_nonce_generation_and_validation_monotonic(counter: int) -> None:
    nonce, new_counter = generate_nonce_with_counter(counter)
    assert len(nonce) == protocol.AEAD_NONCE_SIZE
    assert new_counter == counter + 1
    assert extract_nonce_counter(nonce) == new_counter
    valid, validated_counter = validate_nonce_counter(nonce, counter)
    assert valid is True
    assert validated_counter == new_counter


@given(
    counter=st.integers(min_value=0, max_value=protocol.NONCE_COUNTER_MASK - 1),
    seen_offset=st.integers(min_value=0, max_value=1000),
)
def test_nonce_replay_protection(counter: int, seen_offset: int) -> None:
    nonce, new_counter = generate_nonce_with_counter(counter)
    valid, last = validate_nonce_counter(nonce, new_counter + seen_offset)
    assert valid is False
    assert last == new_counter + seen_offset


@given(
    counter=st.one_of(
        st.integers(max_value=-1),
        st.integers(min_value=protocol.NONCE_COUNTER_MASK, max_value=2**64),
    )
)
@example(counter=protocol.NONCE_COUNTER_MASK)
def test_generate_nonce_overflow_rejection(counter: int) -> None:
    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(counter)


@given(raw=st.binary().filter(lambda b: len(b) != protocol.AEAD_NONCE_SIZE))
def test_extract_and_validate_nonce_invalid_length(raw: bytes) -> None:
    with pytest.raises(ValueError, match=f"Nonce must be {protocol.AEAD_NONCE_SIZE} bytes"):
        extract_nonce_counter(raw)
    valid, last = validate_nonce_counter(raw, 0)
    assert valid is False
    assert last == 0


def test_validate_nonce_counter_overflow_mask(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.security.security.extract_nonce_counter", return_value=protocol.NONCE_COUNTER_MASK + 1)
    valid, last = validate_nonce_counter(b"\x00" * 12, 10)
    assert valid is False
    assert last == 10


def test_verify_crypto_integrity_success() -> None:
    assert verify_crypto_integrity() is True


def test_verify_crypto_integrity_sha256_failure(mocker: MockerFixture) -> None:
    mocker.patch("cryptography.hazmat.primitives.hashes.Hash.finalize", return_value=b"wrong_hash")
    assert verify_crypto_integrity() is False


def test_verify_crypto_integrity_hmac_failure(mocker: MockerFixture) -> None:
    mocker.patch("cryptography.hazmat.primitives.hmac.HMAC.finalize", return_value=b"wrong_hmac")
    assert verify_crypto_integrity() is False


def test_verify_crypto_integrity_chacha_failure(mocker: MockerFixture) -> None:
    mocker.patch(
        "cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt",
        side_effect=ValueError("ChaCha error"),
    )
    assert verify_crypto_integrity() is False
