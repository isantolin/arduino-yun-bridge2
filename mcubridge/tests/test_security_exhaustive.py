"""
This file is part of Arduino MCU Ecosystem v2.
Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from unittest.mock import patch

from hypothesis import given, strategies as st
import pytest

from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame, parse_frame
from mcubridge.security.security import (
    extract_nonce_counter,
    generate_nonce_with_counter,
    secure_zero,
    validate_nonce_counter,
    verify_crypto_integrity,
)


def test_security_nonce_validation_via_public_api() -> None:
    """Verify AEAD round-trip and monotonic nonce validation through public APIs."""
    key = b"\x0f" * protocol.AEAD_KEY_SIZE
    cmd_id = 0x0001
    seq_id = 0x0001
    payload = b"AUTHENTICATED_DATA_PAYLOAD"

    nonce, next_counter = generate_nonce_with_counter(41)

    assert next_counter == 42
    assert len(nonce) == protocol.AEAD_NONCE_SIZE
    assert extract_nonce_counter(nonce, expected_prefix=protocol.MPU_AEAD_NONCE_PREFIX) == next_counter
    prefix_size = protocol.AEAD_NONCE_SIZE - protocol.HANDSHAKE_NONCE_COUNTER_BYTES
    mpu_prefix = protocol.MPU_AEAD_NONCE_PREFIX.to_bytes(prefix_size, "big")
    mcu_prefix = protocol.MCU_AEAD_NONCE_PREFIX.to_bytes(prefix_size, "big")
    assert mpu_prefix != mcu_prefix
    assert nonce[:prefix_size] == mpu_prefix

    raw_frame = build_frame(
        command_id=cmd_id,
        sequence_id=seq_id,
        payload=payload,
        nonce=nonce,
        session_key=key,
    )
    decoded = parse_frame(raw_frame, session_key=key)

    assert decoded.payload == payload

    peer_nonce = mcu_prefix + nonce[prefix_size:]
    peer_frame = build_frame(
        command_id=cmd_id,
        sequence_id=seq_id,
        payload=payload,
        nonce=peer_nonce,
        session_key=key,
    )
    assert parse_frame(peer_frame, session_key=key).payload == payload

    # Anti-reflection validation: MPU-originated nonce rejected when validating incoming MCU frame
    reflected_valid, _ = validate_nonce_counter(nonce, 41)
    assert not reflected_valid

    # Validated with explicit expected MPU emission prefix
    is_valid, updated_counter = validate_nonce_counter(nonce, 41, expected_prefix=protocol.MPU_AEAD_NONCE_PREFIX)
    assert is_valid
    assert updated_counter == next_counter

    # MCU-originated peer nonce succeeds with default expected MCU prefix
    peer_valid, peer_counter = validate_nonce_counter(peer_nonce, 41)
    assert peer_valid
    assert peer_counter == next_counter

    replay_valid, replay_counter = validate_nonce_counter(peer_nonce, next_counter)
    assert not replay_valid
    assert replay_counter == next_counter

    malformed_valid, malformed_counter = validate_nonce_counter(peer_nonce[:-1], 41)
    assert not malformed_valid
    assert malformed_counter == 41


def test_security_nonce_overflow_boundary_public_api() -> None:
    """Verify the final representable counter is emitted and the next one is rejected."""
    nonce, next_counter = generate_nonce_with_counter(protocol.NONCE_COUNTER_MASK - 1)

    assert next_counter == protocol.NONCE_COUNTER_MASK
    assert len(nonce) == protocol.AEAD_NONCE_SIZE
    assert extract_nonce_counter(nonce, expected_prefix=protocol.MPU_AEAD_NONCE_PREFIX) == protocol.NONCE_COUNTER_MASK

    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(protocol.NONCE_COUNTER_MASK)


def test_security_nonce_monotonic_sequence_public_api() -> None:
    """Verify increasing counters pass while repeated and older counters fail."""
    prefix_size = protocol.AEAD_NONCE_SIZE - protocol.HANDSHAKE_NONCE_COUNTER_BYTES
    mcu_prefix = protocol.MCU_AEAD_NONCE_PREFIX.to_bytes(prefix_size, "big")
    raw_nonce_100, counter_100 = generate_nonce_with_counter(99)
    nonce_100 = mcu_prefix + raw_nonce_100[prefix_size:]
    raw_nonce_101, counter_101 = generate_nonce_with_counter(counter_100)
    nonce_101 = mcu_prefix + raw_nonce_101[prefix_size:]

    assert counter_100 == 100
    assert counter_101 == 101

    first_valid, first_counter = validate_nonce_counter(nonce_100, 0)
    assert first_valid
    assert first_counter == counter_100

    second_valid, second_counter = validate_nonce_counter(nonce_101, first_counter)
    assert second_valid
    assert second_counter == counter_101

    replay_valid, replay_counter = validate_nonce_counter(nonce_100, second_counter)
    assert not replay_valid
    assert replay_counter == second_counter


def test_security_crypto_integrity_public_api() -> None:
    """Verify the cryptographic Known Answer Tests remain green."""
    assert verify_crypto_integrity()


@given(data=st.binary(min_size=1, max_size=128))
def test_secure_zero_property(data: bytes) -> None:
    """Verify secure_zero clears bytearray and memoryview buffers in-place."""
    buf = bytearray(data)
    secure_zero(buf)
    assert all(b == 0 for b in buf)

    mv_buf = bytearray(data)
    secure_zero(memoryview(mv_buf))
    assert all(b == 0 for b in mv_buf)


@given(
    counter=st.integers(min_value=0, max_value=protocol.NONCE_COUNTER_MASK - 1),
    prefix=st.sampled_from([protocol.MPU_AEAD_NONCE_PREFIX, protocol.MCU_AEAD_NONCE_PREFIX]),
)
def test_extract_nonce_counter_hypothesis(counter: int, prefix: int) -> None:
    """Verify nonce extraction property across all valid counter boundaries."""
    nonce, new_counter = generate_nonce_with_counter(counter, prefix_val=prefix)
    assert new_counter == counter + 1

    # Extract with expected_prefix=None bypasses prefix checking
    extracted = extract_nonce_counter(nonce, expected_prefix=None)
    assert extracted == new_counter

    # Extract with expected prefix
    assert extract_nonce_counter(nonce, expected_prefix=prefix) == new_counter

    # Extract with mismatched prefix raises ValueError
    mismatched_prefix = 0x99999999
    with pytest.raises(ValueError, match="Nonce prefix mismatch"):
        extract_nonce_counter(nonce, expected_prefix=mismatched_prefix)


@given(bad_nonce=st.binary().filter(lambda b: len(b) != protocol.AEAD_NONCE_SIZE))
def test_extract_nonce_counter_invalid_length_hypothesis(bad_nonce: bytes) -> None:
    """Verify nonce extraction rejects non-12-byte sequences."""
    with pytest.raises(ValueError, match=f"Nonce must be {protocol.AEAD_NONCE_SIZE} bytes"):
        extract_nonce_counter(bad_nonce)


@given(
    counter=st.integers(min_value=0, max_value=protocol.NONCE_COUNTER_MASK - 2),
    step=st.integers(min_value=1, max_value=10),
)
def test_validate_nonce_counter_hypothesis(counter: int, step: int) -> None:
    """Verify monotonicity validation property for nonces."""
    nonce, next_ctr = generate_nonce_with_counter(counter, prefix_val=protocol.MCU_AEAD_NONCE_PREFIX)

    # When current > last_counter: passes
    valid, res_ctr = validate_nonce_counter(nonce, last_counter=counter, expected_prefix=protocol.MCU_AEAD_NONCE_PREFIX)
    assert valid
    assert res_ctr == next_ctr

    # When current <= last_counter: fails
    invalid, kept_ctr = validate_nonce_counter(
        nonce, last_counter=next_ctr + step, expected_prefix=protocol.MCU_AEAD_NONCE_PREFIX
    )
    assert not invalid
    assert kept_ctr == next_ctr + step


def test_verify_crypto_integrity_failure_branches() -> None:
    """Verify all KAT branch failure paths in verify_crypto_integrity."""
    # 1. SHA256 failure
    with patch("cryptography.hazmat.primitives.hashes.Hash.finalize", return_value=b"bad_sha"):
        assert not verify_crypto_integrity()

    # 2. HMAC failure
    with patch("cryptography.hazmat.primitives.hmac.HMAC.finalize", return_value=b"bad_hmac"):
        assert not verify_crypto_integrity()

    # 3. ChaCha20 tag mismatch / length failure
    with patch("cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt", return_value=b"short"):
        assert not verify_crypto_integrity()

    with patch("cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt", return_value=b"x" * 20):
        assert not verify_crypto_integrity()

    # 4. ChaCha20 exception failure
    with patch(
        "cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt", side_effect=ValueError("corrupt")
    ):
        assert not verify_crypto_integrity()
