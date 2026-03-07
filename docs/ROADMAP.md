# Roadmap

> **Current Release**: v2.5.1 (OpenWrt 25.12.0 final compatible)

## Completed (Q1 2026)

### 1. OpenWrt 25.12 & SIL-2 Modernization
- **O(1) C++ Dispatch**: Replaced switch/case with jump tables for deterministic execution.
- **Python 3.13.9-r2**: Full compatibility and optimization using uvloop and msgspec.
- **Mutual Auth Handshake**: Robust HKDF-SHA256 based synchronization with anti-replay protection.
- **MsgPack over MQTT**: Migrated from JSON to binary serialization for ultra-low latency.
- **Native Flow Control**: Implemented software XON/XOFF support in serial transport.
- **Strong Type Safety**: Integrated PEP-561 type stubs for third-party OS libraries (`sh`, `uci`).
- **100% Protocol Sync**: Guaranteed consistency between MPU and MCU via automated code generation.
- **Race Condition Protection**: Hardened FSM to handle high-speed asynchronous responses.

## Future Strategic Goals (2026-2027)

### 1. Lifecycle Management & FOTA
- **Integrated OTA Service**: Automated firmware detection and flashing using `avrdude`/`esptool`.
- **Safe-Bootloader Handshake**: Protocol extension to trigger MCU bootloader mode via RPC.

### 2. Deep Observability
- **Remote Stack Guard**: Real-time reporting of MCU stack high-water mark and static memory usage to Prometheus.
- **Virtual Oscilloscope**: High-frequency signal streaming from MCU pins to MQTT topics.

### 3. Resilience & Hardware Abstraction
- **Auto-Baudrate Fallback**: Automated speed downgrade logic based on CRC error thresholds.
- **SPI Service**: Full implementation of the SPI capability bit with a dedicated RPC service.
- **Physical Flow Control**: HAL support for RTS/CTS hardware lines for non-XON platforms.

### 4. Zero-Code Experience
- **Dynamic LuCI UI**: Automated web interface generation based on `spec.toml` definitions.
- **Pythonic MCU Mocking**: Local development library that transparently uses the emulator when hardware is missing.
