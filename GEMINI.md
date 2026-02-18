# GEMINI.md: Your AI Assistant's Guide to Arduino MCU Bridge 2

This document provides a comprehensive overview of the Arduino MCU Bridge 2 project, designed to be used as a context file for AI assistants like Gemini.

## Project Overview

Arduino MCU Bridge 2 is a modern, high-performance replacement for the original Arduino MCU Bridge system. It facilitates robust and efficient communication between the Arduino-compatible microcontroller (MCU) and the Linux-based microprocessor (MPU) on the Arduino MCU and compatible boards.

The core of the project is a Python daemon that runs on the Linux MPU and a C++ library that runs on the Arduino MCU. They communicate over the serial port using a custom, efficient, and reliable binary RPC protocol. The protocol is formally defined in a TOML file, and the corresponding C++ and Python code is generated from this specification, ensuring consistency between the two sides of the bridge.

The project also includes a LuCI web interface for configuration and monitoring, as well as example code for both Python and Arduino.

### Key Technologies

*   **Python:** The main daemon on the Linux MPU is written in Python (3.13.9-r2), using `asyncio` for high-performance, non-blocking I/O.
*   **C++:** The library for the Arduino MCU is written in C++11, strictly adhering to **SIL-2** safety standards (no STL, no dynamic memory) and **MIL-SPEC** (FIPS 140-3) for cryptographic integrity.
*   **Cryptography:** Implements **HKDF-SHA256** (RFC 5869) for key derivation and mandatory **Power-On Self-Tests (POST/KAT)** at startup.
*   **Lua:** The LuCI web interface is written in Lua.
*   **OpenWrt:** The target operating system is **OpenWrt 25.12.0** (APK based).
*   **MQTT:** The bridge uses MQTT v5 for communication with other devices.
*   **TOML:** The communication protocol is defined in a TOML file.

## Building and Running

The project provides a set of shell scripts to automate the build, deployment, and installation process.

### Building

To build the project for OpenWrt 25.12.0, run the `1_compile.sh` script. This script will:

1.  Download and configure the OpenWrt SDK (25.12.0).
2.  Copy the project's packages into the SDK.
3.  Compile the packages to create `.apk` files (modern package format).
4.  Place the resulting `.apk` files in the `bin/` directory.

```bash
./1_compile.sh
```

### Expanding the Filesystem (Optional)

If your device has limited storage, you can use the `2_expand.sh` script to expand the filesystem onto a microSD card or USB drive.

```bash
./2_expand.sh
```

### Installing

To install the bridge on your Arduino MCU, transfer the project files to the device and run the `3_install.sh` script as root. This script will:

1.  Install the necessary system dependencies.
2.  Install the project's `.apk` packages.
3.  Configure the system.
4.  Start the `mcubridge` daemon.

```bash
./3_install.sh
```

### Testing

The project uses `tox` to run a comprehensive suite of tests. To run the tests, simply run the `tox` command in the root of the project. This will run unit tests, linting, type checking, and more.

```bash
tox
```

## Development Conventions

The project follows modern, best-practice development conventions:

*   **Code Formatting:** Python code is formatted with `black`.
*   **Import Sorting:** Python imports are sorted with `isort`.
*   **Linting:** The code is linted with `ruff` and `flake8`.
*   **Static Type Checking:** Python code is type-checked with `pyright`.
*   **Protocol as Code:** The communication protocol is defined in `tools/protocol/spec.toml` and the corresponding code is generated using `tools/protocol/generate.py`. This ensures that the protocol is always in sync between the C++ and Python codebases.
*   **Architecture:** Data structures are centralized in `mcubridge/protocol/structures.py` to serve as a single source of truth, utilizing `construct` for binary schemas and `msgspec` for typed structs. This implementation is synchronized with `tools/protocol/spec.toml`.
    *   **Frame Layer:** Uses full `construct` integration with `Checksum` for automatic CRC32 validation and `Switch` for payload schema resolution.
    *   **Packet Layer:** Uses full `construct` + `msgspec` validation (including `ge=0` checks) to parse payloads into typed objects on demand.
*   **Observability:** Built-in Prometheus exporter exposes extensive runtime metrics, including task supervisor health (restarts, backoff derived from native `tenacity` statistics), queue depths, serial latency histograms, and I/O throughput.
*   **Refactoring Status:** The Python codebase has completed Phase 3 "Mechanical Refactoring", fully migrating all services (Process, Console, Pin, Handshake, File, Datastore, Mailbox) to use typed `BaseStruct` packets with `msgspec` validation (e.g., `ge=0`) for robust binary handling. The C++ library (`openwrt-library-arduino`) has been verified to be compatible with the updated protocol (ETL-based, SIL-2 compliant, Handshake size aligned) and has started the migration to generated C++ structs for safer parsing.
*   **Automated CI/CD:** The project uses GitHub Actions to automate the build and test process.
