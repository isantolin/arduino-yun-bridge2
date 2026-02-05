"""Security primitives for military-grade cryptographic operations."""

from .security import (
    derive_handshake_key,
    hkdf_sha256,
    secure_zero,
    secure_zero_bytes_copy,
    generate_nonce_with_counter,
    extract_nonce_counter,
    validate_nonce_counter,
    verify_crypto_integrity,
    NONCE_RANDOM_BYTES,
    NONCE_COUNTER_BYTES,
    NONCE_TOTAL_BYTES,
)

__all__ = [
    "derive_handshake_key",
    "hkdf_sha256",
    "secure_zero",
    "secure_zero_bytes_copy",
    "generate_nonce_with_counter",
    "extract_nonce_counter",
    "validate_nonce_counter",
    "verify_crypto_integrity",
    "NONCE_RANDOM_BYTES",
    "NONCE_COUNTER_BYTES",
    "NONCE_TOTAL_BYTES",
]
