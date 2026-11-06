# Roadmap

> **Current Release**: v2.8.8 (OpenWrt 25.12.6 final compatible)

## Completed (Q1-Q3 2026)

### 1. Security & Architecture Overhaul (v2.8.5)
- **Authenticated encrypted MCU link**: Pigweed provides HDLC/RPC transport; PSA provides SHA-256/HMAC, while Mbed TLS 4 (AVR/SAMD) and wolfCrypt 5.8.4 (ESP32) provide HKDF and ChaCha20-Poly1305. Per-session counters reject replay within the live session; cross-restart replay freshness remains a limitation.
- **Zero-Wrapper Architecture**: Total flattening of the Python daemon; eradicated 8 redundant component classes for a lean, direct `BridgeService`.
- **Integrated Flow Control**: Merged `SerialFlowController` into `SerialTransport` with native `tenacity` retry support.
- **OpenWrt 25.12.6 Support**: Hardened for the latest APK-based OpenWrt release.
- **100% E2E Coverage**: Restored and expanded end-to-end testing to cover all example clients.

### 2. OpenWrt 25.12 & Safety-Oriented Modernization (C++17)
- **C++17 Migration**: Leveraging modern language features (lambdas, structured bindings) for cleaner, safer code.
- **Safety-oriented implementation work**: C-style casts replaced with `reinterpret_cast`/`static_cast` and memory-safety checks added; these changes do not establish SIL-2 certification.
- **O(1) C++ Dispatch**: Replaced switch/case with jump tables for deterministic execution.
- **Python 3.13.9+**: Full compatibility and optimization using uvloop and Protobuf.
- **Link authentication and protected HDLC transport**: HKDF-SHA256/HMAC-SHA256 request and response proofs derive directional ChaCha20-Poly1305 session keys. A monotonically increasing counter per direction protects RPC, console, and log frames against replay within a session; replay across MCU restarts is not ruled out.
- **Protobuf over gRPC**: Migrated from JSON to protobuf payloads for deterministic binary interoperability.
- **Strong Type Safety**: Integrated PEP-561 type stubs for third-party libraries.
- **100% Protocol Sync**: Guaranteed consistency between MPU and MCU via automated code generation.
- **Protobuf Payload Serialization**: All RPC payloads migrated to protobuf/nanopb with zero-heap codecs.
- **Race Condition Protection**: Hardened FSM to handle high-speed asynchronous responses.

### 3. Lifecycle Management, Wireless Expansion & Hardware Resilience (Q3 2026)
- **Transparent Wireless Transports**: Native WiFi TCP stream (`AsyncTcpConnection`, `WiFiClient`) and Bluetooth SPP/BLE UART (`BluetoothSerial`).
- **Nanopb 0.4.9.2 Zero-Copy Deserialization**: Ecosystem upgrade with `pb_decode_noinit` eliminating redundant memory operations on static buffers.
- **TLS 1.3 Native Session Resumption**: Standard OpenSSL and Python standard library delegation for fast gRPC reconnection upon network recovery without custom LMDB ticket shims.
- **Host Benchmarking & Memory Profiling**: `tools/profiling/benchmark_performance.py` measures Python/Linux framing, standalone AEAD primitives, Protobuf, and LMDB throughput plus host-process memory. These results do not establish active MCU frame encryption, AVR performance, MCU SRAM use, or WCET.
- **Safe-Bootloader Handshake**: Protocol extension to trigger MCU bootloader mode via RPC.
- **Auto-Baudrate Fallback**: Automated speed downgrade logic based on CRC error thresholds.
- **SPI Service**: Full implementation of the SPI capability bit with a dedicated RPC service.

### 4. Deterministic FSM Ecosystem & Declarative Resilience (v2.8.7 - Q3 2026 - RELEASED)
- **Ecosystem-Wide `python-statemachine`**: Explicit lifecycle state machines across selected boundaries; this is not a formal SIL-2 assessment:
  - `HandshakeMachine`: Eradicated manual state mapping and glue code; registered `SerialHandshakeManager` as listener with native hooks (`on_enter_synchronized`, `on_exit_synchronized`, `after_transition`).
  - `LinkConnectionMachine`: Strongly-typed physical connection states (`disconnected`, `connected`, `synchronized`) with idempotent transitions.
  - `ProcessMachine`: Deterministic subprocess lifecycle states (`spawning`, `running`, `terminating`, `exited`) with graceful termination escalation.
  - `GatewaySessionMachine`: Formal session lifecycle tracking in Cloud Gateway (`connected`, `authenticated`, `active`, `closed`).
- **Declarative Retries & Exponential Backoff (`tenacity`)**: Universal `AsyncRetrying` integration across serial transport, handshake protocol, cloud reconnection, UBUS reconnection, and supervised daemon tasks with jittered backoff.
- **Atomic TOCTOU-Free Local Storage I/O**: Consolidated path creation and file writes in unified thread workers (`_sync_write_file`), cutting context switching overhead in half, alongside native dictionary-like primitives in `LmdbCache`.
- **OpenWrt Native UBUS Integration (v2.8.6)**: Full UBUS RPC interface (`ubus call mcubridge ...`), native LuCI ucode dispatch, and direct hardware control (< 2 ms latency).
- **Process Management with `psutil`**: Full replacement of `/proc` scraping and `killpg` with deterministic process tree signaling and memory telemetry.

### 5. Google Pigweed (`pw_hdlc` + `pw_rpc`) Architecture Migration (v2.8.9 - Q4 2026)
- **Pigweed HDLC Link Layer (`pw_hdlc`)**: Complete replacement of legacy single-channel COBS framing with 3-channel `pw_hdlc` UI frames (`HDLC_ADDRESS_RPC = 1`, `HDLC_ADDRESS_LOG = 2`, `HDLC_ADDRESS_CONSOLE = 3`) protected by IEEE 802.3 CRC-32 FCS.
- **Native `pw_rpc` Engine & `PW_SERVICES` SSOT**:
  - MCU firmware uses upstream `pw::rpc::Server` / `pw::rpc::ClientServer` over `pw::rpc::Channel` and Nanopb stubs generated by `tools/protocol/protoc_gen_mcu_rpc.py` (`mcu_wire.rpc.pb.h`), registering 6 `rpc::pb::pw_rpc::nanopb::<Service>::Service` server implementations (`GpioService`, `SystemService`, `ClockSyncService`, `SpiService`, `FileSystemService`, and `MailboxService`) with compile-time `pw::tokenizer::Hash` identifiers. `ProcessService` and `DataStoreService` remain in the shared Protobuf contract (`rpc::pb::pw_rpc::nanopb::<Service>::Client`) for MCU-initiated requests to the Linux daemon; they are not registered as MCU RPC server services. Pin updates use `pw::rpc::NanopbServerWriter` / the `SERVER_STREAM` event path on `GpioService/SubscribePins`.
  - MPU daemon uses `pw_rpc.descriptors.Services` (`PW_SERVICES`) and `pw_rpc.packets` directly for RPC descriptor resolution and packet encoding/decoding.
- **Embedded Pigweed Modules (`PigweedAVR`)**: Integrated `pw::chrono::SystemClock::now()` (`pw_chrono_avr`) for atomic millisecond timekeeping and `pw::log::SetLogSink` (`pw_log_avr` / `pw_tokenizer`) decoded via `pw_tokenizer.Detokenizer`, maintaining strict Zero-STL (`-DETL_NO_STL`) and Zero-Heap compliance across AVR, SAMD21, and ESP32-S3.

## Future Strategic Goals (2026-2027)

### 1. Pigweed crypto consolidation
- The authenticated encrypted MCU link is implemented; preserve its wire format, POST, and fail-closed behavior while evaluating provider consolidation.
- Prefer a maintained Pigweed AEAD API/backend only after confirming support for AVR, SAMD, and ESP32 and measuring Flash, SRAM, and runtime costs. The current Pigweed ChaCha20 API is a stream cipher, and its BoringSSL backend is marked incompatible with MCU targets; neither provides MCU ChaCha20-Poly1305 in this export.
- Any provider change still requires compatible MCU support, pinned dependencies, and measured Flash/SRAM/runtime costs. Do not construct Poly1305 or AEAD manually.

### 1. Zero-Code Experience
- **Dynamic LuCI UI**: Automated web interface generation based on modular proto definitions.
- **Pythonic MCU Mocking**: Local development library that transparently uses the emulator when hardware is missing.
