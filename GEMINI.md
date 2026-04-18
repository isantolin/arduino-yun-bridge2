# GEMINI.md: Your AI Assistant's Guide to Arduino MCU Bridge 2 (SIL-2/MIL-SPEC)

This document provides a comprehensive overview of the Arduino MCU Bridge 2 project, optimized for AI agents following aerospace and medical engineering standards.

## Project Overview

Arduino MCU Bridge 2 is a modern, high-performance communication system between Arduino-compatible microcontrollers (MCU) and Linux-based microprocessors (MPU). It adheres to **SIL-2** safety standards and **MIL-SPEC** (FIPS 140-3) integrity.

### Key Technologies & Standards

*   **Python:** Main daemon (3.13.9+), `asyncio` for high-concurrency, `msgspec` for MsgPack.
*   **C++:** Arduino library (C++17), **Zero-Heap** (no STL, no `malloc`), `etl::fsm` for deterministic states, `etl::observer` for decoupling.
*   **Safety (SIL-2):** Static allocation only, O(1) jump tables for dispatch, rigorous validation gates.
*   **OpenWrt:** Target OS is **OpenWrt 25.12.2** (APK based).
*   **Communication:** Custom binary RPC over serial (COBS + CRC32) + MQTT v5 (aiomqtt).

## Development Conventions

### Python (Linux MPU)
*   **Direct Library Calls:** Zero-wrapper policy. Use libraries directly (e.g., `aiomqtt`, `paho.mqtt`) instead of custom abstraction layers.
*   **Strict Typing:** `pyright` in strict mode. All tests must use `unittest.mock.AsyncMock(spec=Interface)`.
*   **No "Dummy" Classes:** Manual mock classes are prohibited in favor of standardized `AsyncMock`.
*   **Async Patterns:** Mandatory `asyncio` usage; no blocking calls in the main event loop.

### C++ (Arduino MCU)
*   **Zero-Heap:** Strictly no dynamic memory allocation. All structures and buffers are statically sized.
*   **ETL Integration:** Leverage the Embedded Template Library for data structures and logic.
*   **FSM:** Implement state logic using `etl::fsm` for deterministic transitions.
*   **Observer:** Components register as observers for system events (e.g., `on_frame`, `on_reset`).

### Protocol as Code
*   **Specification:** `tools/protocol/spec.toml` (constants, enums) and `tools/protocol/mcubridge.proto` (schemas).
*   **Generation:** `tools/protocol/generate.py` produces both Python (`structures.py`) and C++ (`rpc_structs.h`) bindings.
*   **Serialization:** **MsgPack** (array format) for all payloads. Python uses `msgspec`, C++ uses a header-only, static MsgPack codec.

## Building and Running

### Build Pipeline
1.  **Compile:** `./1_compile.sh` for OpenWrt APK creation.
2.  **Install:** `./3_install.sh` on target device.
3.  **Validate:** `tox` runs all unit tests; `tox -e e2e` runs full system integration tests against a native C++ emulator.

### Observability
*   **Metrics:** Prometheus exporter on port 8000.
*   **Tracing:** Structured hex logs `[MCU -> SERIAL]` for auditability.
*   **Watchdog:** Hardware-backed watchdog support with heartbeat monitoring.

## Status

The ecosystem is fully refactored and modernized. Primary service components utilize `AsyncMock` for testing, ensuring high interface fidelity. The C++ library follows strict SIL-2 guidelines with O(1) dispatching. End-to-end testing verifies the complete integration between the Python daemon and the C++ logic.
