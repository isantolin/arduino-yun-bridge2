# ADR-0006: WolfCrypt Link Crypto Provider

- Status: Accepted (amended)
- Date: 2026-11-03; amended 2026-11-16 and 2026-11-26
- Renumbered: originally published as a second ADR-0003, which collided with [ADR-0003: Pigweed gateway replacement](0003-pigweed-gateway-replacement.md). The content below the amendment is the original decision.
- Decision owners: Repository maintainer

## Amendments (2026-11-16 and 2026-11-26)

wolfCrypt 5.8.4 remains the sole cryptographic provider on AVR, SAMD21 and ESP32 firmware: SHA-256, HMAC-SHA256, HKDF-SHA256, ChaCha20-Poly1305 and zeroization (`ForceZero`) come from wolfCrypt (`security.cpp`). Mbed TLS and PSA are no longer used by the firmware. The Arduino package remains pinned separately to `wolfSSL/Arduino-wolfSSL` 5.8.4 and uses the MCU-specific `WOLFCRYPT_ONLY`, no-malloc settings.

The Linux Gateway now uses the full wolfSSL 5.9.4 TLS implementation with wolfCrypt as its cryptographic backend. It provides TLS 1.3 only, mandatory client-certificate authentication, CA-chain verification, client-auth EKU enforcement, and the existing single-URI-SAN identity rules. The exact full-source archive and SHA-256 are pinned identically in CMake and Bazel. The Linux build does not reuse the MCU `WOLFCRYPT_ONLY` settings.

OpenSSL has been removed from the Gateway executable, its CMake/Bazel link dependencies, and the production container. Python's TLS test client and the test-only `openssl` command used to generate certificate fixtures are test tooling, not Gateway runtime dependencies. wolfSSL source is distributed under GPLv3; distribution of the Gateway and its corresponding source must meet the applicable license obligations. This amendment does not claim FIPS validation or certification.

The original decision below is retained as historical context; its provider split and implementation guidance are superseded by the amendments above.

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
