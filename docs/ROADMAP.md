# Roadmap

> **Current Release**: v2.8.5 (OpenWrt 25.12.3 final compatible)

## Completed (Q1-Q2 2026)

### 1. Security & Architecture Overhaul (v2.8.5)
- **ChaCha20-Poly1305 AEAD**: Full ecosystem migration to authenticated encryption with original header preservation (Zero-Copy AD).
- **Zero-Wrapper Architecture**: Total flattening of the Python daemon; eradicated 8 redundant component classes for a lean, direct `BridgeService`.
- **Integrated Flow Control**: Merged `SerialFlowController` into `SerialTransport` with native `tenacity` retry support.
- **OpenWrt 25.12.3 Support**: Hardened for the latest APK-based OpenWrt release.
- **100% E2E Coverage**: Restored and expanded end-to-end testing to cover all example clients.

### 2. OpenWrt 25.12 & SIL-2 Modernization (C++17)
- **C++17 Migration**: Leveraging modern language features (lambdas, structured bindings) for cleaner, safer code.
- **Strict SIL-2 Compliance**: All C-style casts replaced with `reinterpret_cast`/`static_cast` and reinforced memory safety.
- **O(1) C++ Dispatch**: Replaced switch/case with jump tables for deterministic execution.
- **Python 3.13.9+**: Full compatibility and optimization using uvloop and msgspec.
- **Mutual Auth Handshake**: Robust HKDF-SHA256 based synchronization with anti-replay protection.
- **MsgPack over MQTT**: Migrated from JSON to binary serialization for ultra-low latency.
- **Strong Type Safety**: Integrated PEP-561 type stubs for third-party libraries.
- **100% Protocol Sync**: Guaranteed consistency between MPU and MCU via automated code generation.
- **MsgPack Payload Serialization**: All RPC payloads migrated to MsgPack (array format) with zero-heap codecs.
- **Race Condition Protection**: Hardened FSM to handle high-speed asynchronous responses.

### 3. Lifecycle Management & Hardware Resilience
- **Safe-Bootloader Handshake**: Protocol extension to trigger MCU bootloader mode via RPC.
- **Auto-Baudrate Fallback**: Automated speed downgrade logic based on CRC error thresholds.
- **SPI Service**: Full implementation of the SPI capability bit with a dedicated RPC service.

## Future Strategic Goals (2026-2027)

### 1. Zero-Code Experience
- **Dynamic LuCI UI**: Automated web interface generation based on `spec.toml` definitions.
- **Pythonic MCU Mocking**: Local development library that transparently uses the emulator when hardware is missing.
