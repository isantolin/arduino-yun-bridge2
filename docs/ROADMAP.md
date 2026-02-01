# Roadmap

> **Current Release**: v2.5.1 (OpenWrt 25.12.0 final compatible)

## Completed

### OpenWrt 25.12 Integration ✅ (Feb 2026)
- Full migration to APK-based package system (`apk` replaces `opkg`).
- Native UCI configuration with fail-fast validation.
- Flash wear protection: all transient paths enforced under `/tmp` (RAM).
- Structured JSON logging via `syslog` with hexadecimal binary dumps.

### SIL-2 Compliance (MCU) ✅ (Feb 2026)
- Zero-STL policy enforced via compile-time guards.
- ETL-based containers: `etl::array`, `etl::vector`, `etl::queue`, `etl::string`, `etl::circular_buffer`.
- No `malloc`/`new`/`delay()` in production code.
- CRC32 integrity on all RPC frames.
- Explicit FSM with deterministic state transitions.

### Python 3.13 Modernization ✅ (Feb 2026)
- Target runtime: Python 3.13.9-r2.
- Mandatory `uvloop` for async I/O performance.
- `msgspec` for high-performance serialization.
- No lambdas/closures/shadowing enforced via AST policy tests.

### MIL-SPEC Security (FIPS 140-3) ✅ (Feb 2026)
- HKDF-SHA256 key derivation for handshake authentication.
- HMAC-SHA256 truncated tags for frame authentication.
- Cryptographic Power-On Self-Tests (POST) with KAT vectors.
- Secure memory wipe with volatile barriers.

### Protocol Generator ✅
- Single source of truth: `tools/protocol/spec.toml`.
- Auto-generated Python (`protocol.py`) and C++ (`rpc_protocol.h`) bindings.
- 100% concordance verified between implementations.

## Pending (Q2-Q3 2026)

- **Hardware Validation Matrix**: Comprehensive testing across AVR, ESP32, ESP8266, SAMD, and RP2040.
- **Production Field Trials**: Extended deployment on real OpenWrt devices.
- **Documentation Expansion**: User guides and integration examples.
