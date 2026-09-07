# Roadmap

> **Current Release**: v2.8.7 (OpenWrt 25.12.5 final compatible)

## Completed (Q1-Q3 2026)

### 1. Security & Architecture Overhaul (v2.8.5)
- **ChaCha20-Poly1305 AEAD**: Full ecosystem migration to authenticated encryption with original header preservation (Zero-Copy AD).
- **Zero-Wrapper Architecture**: Total flattening of the Python daemon; eradicated 8 redundant component classes for a lean, direct `BridgeService`.
- **Integrated Flow Control**: Merged `SerialFlowController` into `SerialTransport` with native `tenacity` retry support.
- **OpenWrt 25.12.5 Support**: Hardened for the latest APK-based OpenWrt release.
- **100% E2E Coverage**: Restored and expanded end-to-end testing to cover all example clients.

### 2. OpenWrt 25.12 & SIL-2 Modernization (C++17)
- **C++17 Migration**: Leveraging modern language features (lambdas, structured bindings) for cleaner, safer code.
- **Strict SIL-2 Compliance**: All C-style casts replaced with `reinterpret_cast`/`static_cast` and reinforced memory safety.
- **O(1) C++ Dispatch**: Replaced switch/case with jump tables for deterministic execution.
- **Python 3.13.9+**: Full compatibility and optimization using uvloop and Protobuf.
- **Mutual Auth Handshake**: Robust HKDF-SHA256 based synchronization with anti-replay protection.
- **Protobuf over gRPC**: Migrated from JSON to protobuf payloads for deterministic binary interoperability.
- **Strong Type Safety**: Integrated PEP-561 type stubs for third-party libraries.
- **100% Protocol Sync**: Guaranteed consistency between MPU and MCU via automated code generation.
- **Protobuf Payload Serialization**: All RPC payloads migrated to protobuf/nanopb with zero-heap codecs.
- **Race Condition Protection**: Hardened FSM to handle high-speed asynchronous responses.

### 3. Lifecycle Management, Wireless Expansion & Hardware Resilience (Q3 2026)
- **Transparent Wireless Transports**: Native WiFi TCP stream (`AsyncTcpConnection`, `WiFiClient`) and Bluetooth SPP/BLE UART (`BluetoothSerial`).
- **Nanopb 0.4.9.2 Zero-Copy Deserialization**: Ecosystem upgrade with `pb_decode_noinit` eliminating redundant memory operations on static buffers.
- **TLS 1.3 0-RTT Session Ticket Persistence**: LMDB transactional session caching for zero-roundtrip gRPC reconnection upon network recovery.
- **Unified Benchmarking & Memory Profiling Suite**: Deterministic performance measurement tools (`tools/profiling/benchmark_performance.py`) validating framing throughput (> 460k ops/s), AEAD (> 155k ops/s), and sub-2MiB memory footprint.
- **Safe-Bootloader Handshake**: Protocol extension to trigger MCU bootloader mode via RPC.
- **Auto-Baudrate Fallback**: Automated speed downgrade logic based on CRC error thresholds.
- **SPI Service**: Full implementation of the SPI capability bit with a dedicated RPC service.

### 4. Deterministic FSM Ecosystem & Declarative Resilience (v2.8.7 - Q3 2026)
- **Ecosystem-Wide `python-statemachine`**: Formal SIL-2 deterministic state machines across all critical lifecycle boundaries:
  - `HandshakeMachine`: Eradicated manual state mapping and glue code; registered `SerialHandshakeManager` as listener with native hooks (`on_enter_synchronized`, `on_exit_synchronized`, `after_transition`).
  - `LinkConnectionMachine`: Strongly-typed physical connection states (`disconnected`, `connected`, `synchronized`) with idempotent transitions.
  - `ProcessMachine`: Deterministic subprocess lifecycle states (`spawning`, `running`, `terminating`, `exited`) with graceful termination escalation.
  - `GatewaySessionMachine`: Formal session lifecycle tracking in Cloud Gateway (`connected`, `authenticated`, `active`, `closed`).
- **Declarative Retries & Exponential Backoff (`tenacity`)**: Universal `AsyncRetrying` integration across serial transport, handshake protocol, cloud reconnection, UBUS reconnection, and supervised daemon tasks with jittered backoff.
- **Atomic TOCTOU-Free Local Storage I/O**: Consolidated path creation and file writes in unified thread workers (`_sync_write_file`), cutting context switching overhead in half, alongside native dictionary-like primitives in `LmdbCache`.
- **OpenWrt Native UBUS Integration (v2.8.6)**: Full UBUS RPC interface (`ubus call mcubridge ...`), native LuCI ucode dispatch, and direct hardware control (< 2 ms latency).
- **Process Management with `psutil`**: Full replacement of `/proc` scraping and `killpg` with deterministic process tree signaling and memory telemetry.

## Future Strategic Goals (2026-2027)

### 1. Zero-Code Experience
- **Dynamic LuCI UI**: Automated web interface generation based on `mcubridge.proto` definitions.
- **Pythonic MCU Mocking**: Local development library that transparently uses the emulator when hardware is missing.
