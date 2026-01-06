# GEMINI.md: Your AI Assistant's Guide to Arduino Yún Bridge 2

This document provides a comprehensive overview of the Arduino Yún Bridge 2 project, designed to be used as a context file for AI assistants like Gemini.

## Project Overview

Arduino Yún Bridge 2 is a modern, high-performance replacement for the original Arduino Yún Bridge system. It facilitates robust and efficient communication between the Arduino-compatible microcontroller (MCU) and the Linux-based microprocessor (MPU) on the Arduino Yún and compatible boards.

The core of the project is a Python daemon that runs on the Linux MPU and a C++ library that runs on the Arduino MCU. They communicate over the serial port using a custom, efficient, and reliable binary RPC protocol. The protocol is formally defined in a TOML file, and the corresponding C++ and Python code is generated from this specification, ensuring consistency between the two sides of the bridge.

The project also includes a LuCI web interface for configuration and monitoring, as well as example code for both Python and Arduino.

### Key Technologies

*   **Python:** The main daemon on the Linux MPU is written in Python, using `asyncio` for high-performance, non-blocking I/O.
*   **C++:** The library for the Arduino MCU is written in C++.
*   **Lua:** The LuCI web interface is written in Lua.
*   **OpenWrt:** The target operating system for the Linux MPU is OpenWrt.
*   **MQTT:** The bridge uses MQTT for communication with other devices and services on the network.
*   **TOML:** The communication protocol is defined in a TOML file.

## Building and Running

The project provides a set of shell scripts to automate the build, deployment, and installation process.

### Building

To build the project, run the `1_compile.sh` script. This script will:

1.  Download and configure the OpenWrt SDK.
2.  Copy the project's packages into the SDK.
3.  Compile the packages to create `.apk` files.
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

To install the bridge on your Arduino Yún, transfer the project files to the device and run the `3_install.sh` script as root. This script will:

1.  Install the necessary system dependencies.
2.  Install the project's `.apk` packages.
3.  Configure the system.
4.  Start the `yunbridge` daemon.

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
*   **Automated CI/CD:** The project uses GitHub Actions to automate the build and test process.
