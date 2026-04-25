# GEMINI.md: Your AI Assistant's Guide to Arduino MCU Bridge 2 (SIL-2/MIL-SPEC)

This document provides a comprehensive overview of the Arduino MCU Bridge 2 project, optimized for AI agents following aerospace and medical engineering standards.

## Project Overview

Arduino MCU Bridge 2 is a modern, high-performance communication system between Arduino-compatible microcontrollers (MCU) and Linux-based microprocessors (MPU). It adheres to **SIL-2** safety standards and **MIL-SPEC** (FIPS 140-3) integrity.

### Key Technologies & Standards

*   **Python:** Main daemon (3.13.9+), `asyncio` for high-concurrency with `uvloop` integration, `msgspec` for MsgPack.
*   **C++:** Arduino library (C++17), **Zero-Heap** (no STL, no `malloc`), `etl::fsm` for deterministic states with **Strongly Typed FSM (StateId)**, `etl::observer` for decoupling.
*   **Safety (SIL-2):** Static allocation only, O(1) jump tables for dispatch, rigorous validation gates.
*   **OpenWrt:** Target OS is **OpenWrt 25.12.0** (APK based).
*   **Communication:** Custom binary RPC over serial (COBS + CRC32) + MQTT v5 (aiomqtt). Protocol validation uses O(1) `etl::find` logic.

## Development Conventions

### Python (Linux MPU)
*   **Direct Library Calls:** Zero-wrapper policy. Use libraries directly (e.g., `aiomqtt`, `paho.mqtt`) instead of custom abstraction layers.
*   **Strict Typing:** `pyright` in strict mode. All tests must use `unittest.mock.AsyncMock(spec=Interface)`.
*   **No "Dummy" Classes:** Manual mock classes are prohibited in favor of standardized `AsyncMock`.
*   **Async Patterns:** Mandatory `asyncio` usage; no blocking calls in the main event loop. `uvloop` is used on target for maximum throughput.

### C++ (Arduino MCU)
*   **Zero-Heap:** Strictly no dynamic memory allocation. All structures and buffers are statically sized using `etl::array`.
*   **Strongly Typed FSM:** State logic implemented via `etl::fsm` using `enum class StateId : uint8_t` for mission-critical determinism and zero narrowing conversions.
*   **O(1) Dispatch:** Protocol verification (`requires_ack`) uses `constexpr` arrays and `etl::find` for constant-time complexity.
*   **Observer:** Components register as observers for system events (e.g., `on_frame`, `on_reset`).

## Building and Running

### Build Pipeline
1.  **Compile:** `./1_compile.sh` for OpenWrt APK creation.
2.  **Install:** `./3_install.sh` on target device.
3.  **Validate:** `tox` runs all unit tests; `tox -e coverage` generates Python (90%+) and C++ (75%+) reports.

### Observability
*   **Metrics:** Prometheus exporter on port 8000.
*   **Tracing:** Structured hex logs `[MCU -> SERIAL]` for auditability via syslog.
*   **Watchdog:** Hardware-backed watchdog support with heartbeat monitoring. Procd watchdog integration in OpenWrt.

## Status

**Current Version:** v2.8.5 - **Flight-Ready**
The ecosystem is fully refactored and modernized. Primary service components utilize `AsyncMock` for testing, ensuring high interface fidelity. The C++ library follows strict SIL-2 guidelines with O(1) dispatching and strong typing. End-to-end testing verifies the complete integration between the Python daemon and the C++ logic.
