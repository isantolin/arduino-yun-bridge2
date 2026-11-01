"""
This file is part of Arduino MCU Ecosystem v2.
Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from typing import Any

import pytest
from pytest_mock import MockerFixture

from mcubridge.security.security import verify_crypto_integrity


def test_verify_crypto_integrity_success() -> None:
    """Verify cryptographic Known Answer Tests (KAT) pass."""
    assert verify_crypto_integrity() is True


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
