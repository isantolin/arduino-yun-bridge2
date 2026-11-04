# ADR-0003: WolfCrypt Link Crypto Provider on ESP32

- Status: Accepted
- Date: 2026-11-03
- Decision owners: Repository maintainer

## Context

The Arduino ESP32 core used by the Nano ESP32 supplies Mbed TLS 2.28.7, whose configured PSA build omits HKDF and ChaCha20-Poly1305. Linking the project's Mbed TLS 4 provider alongside the core causes duplicate symbols. The authenticated link already depends on HKDF-SHA256, HMAC-SHA256, and ChaCha20-Poly1305, and its POST must remain enabled.

The approved Arduino WolfSSL 5.8.4 package provides wolfCrypt APIs for the missing HKDF and AEAD operations. The package is GPL-3.0-or-later; the repository has no root `LICENSE` file, so product distribution must account for the applicable GPL obligations.

## Decision

- Keep PSA/Mbed TLS for SHA-256, HMAC-SHA256, and platform zeroization.
- Use Mbed TLS 4 for HKDF-SHA256 and ChaCha20-Poly1305 on AVR/SAMD; use wolfCrypt 5.8.4 for those primitives on ESP32.
- Pin the official Arduino archive and verify its SHA-256 during installation. Keep the canonical WolfCrypt configuration in the repository, disable malloc, and avoid `WOLFSSL_SMALL_STACK` so the selected operations use stack contexts rather than dynamic allocation.
- Preserve the protocol, KATs, POST enablement, and fail-closed behavior.

## Alternatives considered

- **Link Mbed TLS 4 with the ESP32 core's Mbed TLS 2.28.7:** rejected because the link contains duplicate Mbed TLS symbols.
- **Use the ESP32 core PSA algorithms for every primitive:** rejected because the core build omits HKDF and ChaCha20-Poly1305.
- **Use Pigweed crypto for AEAD:** rejected for this integration because the available MCU port provides ChaCha20 stream operations, not the required ChaCha20-Poly1305 AEAD API.

## Consequences

- ESP32 adds a pinned GPL-3.0-or-later dependency and must retain corresponding license/source-distribution compliance; this ADR does not relicense the repository.
- Host/ESP32 POST and emulator validation are required for changes to either provider. QEMU results do not replace physical hardware validation.
- This provider split does not establish FIPS validation, certification, or an independent security assessment.

## Migration and rollback

- Keep the public wire format and both endpoint contracts unchanged.
- If the Arduino ESP32 core later exposes compatible PSA HKDF and ChaCha20-Poly1305 APIs without link conflicts, evaluate removing the WolfCrypt dependency after behavioral, licensing, resource, and emulator checks.

## Related decisions and implementation

- [ADR-0002: Migration of MCU-MPU Serial Transport and RPC to Google Pigweed](0002-pigweed-hdlc-rpc-migration.md)
- `mcubridge-library-arduino/src/security/security.cpp`
- `requirements/runtime.toml`
