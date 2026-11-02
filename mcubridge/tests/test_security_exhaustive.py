"""
This file is part of Arduino MCU Ecosystem v2.
Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from typing import Any

import pytest
from pytest_mock import MockerFixture

from mcubridge.protocol import protocol
from mcubridge.security.security import LinkCipher, derive_link_keys, verify_crypto_integrity


def test_verify_crypto_integrity_success() -> None:
    """Verify cryptographic Known Answer Tests (KAT) pass."""
    assert verify_crypto_integrity() is True


def test_link_cipher_directional_round_trip() -> None:
    secret = b"link shared secret"
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    host_to_mcu, mcu_to_host = derive_link_keys(secret, nonce)
    host = LinkCipher(host_to_mcu, mcu_to_host)
    mcu = LinkCipher(mcu_to_host, host_to_mcu)

    host_envelope = host.encrypt(protocol.HDLC_ADDRESS_RPC, b"host request")
    assert host_envelope[: protocol.AEAD_COUNTER_SIZE] == b"\x00" * 7 + b"\x01"
    assert mcu.decrypt(protocol.HDLC_ADDRESS_RPC, host_envelope) == b"host request"

    mcu_envelope = mcu.encrypt(protocol.HDLC_ADDRESS_CONSOLE, b"MCU output")
    assert host.decrypt(protocol.HDLC_ADDRESS_CONSOLE, mcu_envelope) == b"MCU output"


def test_link_cipher_authentication_failure_does_not_advance_receive_counter() -> None:
    secret = b"link shared secret"
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    host_to_mcu, mcu_to_host = derive_link_keys(secret, nonce)
    host = LinkCipher(host_to_mcu, mcu_to_host)
    mcu = LinkCipher(mcu_to_host, host_to_mcu)
    envelope = host.encrypt(protocol.HDLC_ADDRESS_RPC, b"request")

    with pytest.raises(ValueError, match="authentication failed"):
        mcu.decrypt(protocol.HDLC_ADDRESS_CONSOLE, envelope)
    assert mcu.decrypt(protocol.HDLC_ADDRESS_RPC, envelope) == b"request"


def test_link_cipher_rejects_replay_and_truncated_frames() -> None:
    secret = b"link shared secret"
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    host_to_mcu, mcu_to_host = derive_link_keys(secret, nonce)
    host = LinkCipher(host_to_mcu, mcu_to_host)
    mcu = LinkCipher(mcu_to_host, host_to_mcu)
    envelope = host.encrypt(protocol.HDLC_ADDRESS_RPC, b"request")

    assert mcu.decrypt(protocol.HDLC_ADDRESS_RPC, envelope) == b"request"
    with pytest.raises(ValueError, match="replayed"):
        mcu.decrypt(protocol.HDLC_ADDRESS_RPC, envelope)
    with pytest.raises(ValueError, match="truncated"):
        mcu.decrypt(protocol.HDLC_ADDRESS_RPC, b"\x00" * (protocol.AEAD_COUNTER_SIZE - 1))


@pytest.mark.parametrize(
    ("target", "kwargs"),
    [
        ("cryptography.hazmat.primitives.hashes.Hash.finalize", {"return_value": b"bad_sha"}),
        ("cryptography.hazmat.primitives.hmac.HMAC.finalize", {"return_value": b"bad_hmac"}),
        ("cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt", {"return_value": b"short"}),
        ("cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt", {"return_value": b"x" * 20}),
        (
            "cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.encrypt",
            {"side_effect": ValueError("corrupt")},
        ),
    ],
)
def test_verify_crypto_integrity_failure_branches(mocker: MockerFixture, target: str, kwargs: dict[str, Any]) -> None:
    """Verify KAT branch failure paths in verify_crypto_integrity using mocker.patch. [SIL-2]"""
    mocker.patch(target, **kwargs)
    assert not verify_crypto_integrity()
