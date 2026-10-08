"""
This file is part of Arduino MCU Ecosystem v2.
Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

import struct
import pytest

from mcubridge.protocol.protocol import (
    NONCE_COUNTER_MASK,
    RPC_AEAD_NONCE_SIZE,
    RPC_AEAD_TAG_SIZE,
)
from mcubridge.security import (
    SecurityError,
    SecurityManager,
    aead_decrypt_frame,
    aead_encrypt_frame,
)


def test_security_nonce_validation_via_public_api() -> None:
    """
    Verifies nonce decoding and validation using the public API
    without using mocker.patch() on internal logic.
    """
    key = b"\x0f" * 32
    cmd_id = 0x0001
    seq_id = 0x0001
    payload = b"AUTHENTICATED_DATA_PAYLOAD"

    counter_val = 42
    ciphertext, nonce, tag = aead_encrypt_frame(
        cmd_id=cmd_id,
        seq_id=seq_id,
        payload=payload,
        key=key,
        counter=counter_val,
    )

    decrypted_payload = aead_decrypt_frame(
        cmd_id=cmd_id,
        seq_id=seq_id,
        ciphertext=ciphertext,
        key=key,
        nonce=nonce,
        tag=tag,
    )

    assert decrypted_payload == payload

    corrupted_nonce = b"BAD" + nonce[3:]
    with pytest.raises((ValueError, SecurityError)):
        aead_decrypt_frame(
            cmd_id=cmd_id,
            seq_id=seq_id,
            ciphertext=ciphertext,
            key=key,
            nonce=corrupted_nonce,
            tag=tag,
        )


def test_security_nonce_overflow_boundary_public_api() -> None:
    """
    Verifies nonce counter upper boundary (2^64 - 1) and rejection of
    overflowed values via the public encryption API.
    """
    key = b"\x1a" * 32
    cmd_id = 0x0002
    seq_id = 0x0001
    payload = b"BOUNDARY_TEST"

    _, nonce, _ = aead_encrypt_frame(
        cmd_id=cmd_id,
        seq_id=seq_id,
        payload=payload,
        key=key,
        counter=NONCE_COUNTER_MASK,
    )
    assert len(nonce) == RPC_AEAD_NONCE_SIZE

    extracted_counter = struct.unpack(">Q", nonce[4:12])[0]
    assert extracted_counter == NONCE_COUNTER_MASK

    with pytest.raises(ValueError, match="Nonce counter overflow"):
        aead_encrypt_frame(
            cmd_id=cmd_id,
            seq_id=seq_id,
            payload=payload,
            key=key,
            counter=NONCE_COUNTER_MASK + 1,
        )


def test_security_manager_replay_attack_prevention() -> None:
    """
    Verifies that SecurityManager prevents replay attacks for old or repeated
    nonces during normal incoming frame decryption.
    """
    secret = b"SECRET_KEY_EXHAUSTIVE_TEST_32B"
    sec_mgr = SecurityManager(secret=secret)
    session_key = b"\x2b" * 32
    sec_mgr.set_session_key(session_key)

    cmd_id = 0x0003
    seq_id = 0x0001
    payload = b"REPLAY_PREVENTION_TEST"

    ct1, nonce1, tag1 = aead_encrypt_frame(
        cmd_id=cmd_id, seq_id=seq_id, payload=payload, key=session_key, counter=100
    )
    ct2, nonce2, tag2 = aead_encrypt_frame(
        cmd_id=cmd_id, seq_id=seq_id, payload=payload, key=session_key, counter=101
    )

    res1 = sec_mgr.decrypt_incoming_frame(
        cmd_id=cmd_id, seq_id=seq_id, ciphertext=ct1, nonce=nonce1, tag=tag1
    )
    assert res1 == payload

    res2 = sec_mgr.decrypt_incoming_frame(
        cmd_id=cmd_id, seq_id=seq_id, ciphertext=ct2, nonce=nonce2, tag=tag2
    )
    assert res2 == payload

    with pytest.raises(SecurityError):
        sec_mgr.decrypt_incoming_frame(
            cmd_id=cmd_id, seq_id=seq_id, ciphertext=ct1, nonce=nonce1, tag=tag1
        )
