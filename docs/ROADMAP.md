# Roadmap

> **Current Release**: v2.5.1 (OpenWrt 25.12.0 final compatible)

## Pending (Q2-Q3 2026)

### 1. OpenWrt Deployment Validation
- **APK Packaging**: Final validation of `.apk` binaries for OpenWrt 25.12, ensuring all Python 3.13 dependencies (uvloop, msgspec) are correctly bundled.
- **Production Field Trials**: Extended deployment on real Arduino Yun and compatible OpenWrt hardware to verify long-term stability.

### 2. Hardware & Architecture Expansion
- **Hardware Validation Matrix**: Comprehensive testing and CI integration for non-AVR architectures: **ESP32**, **ESP8266**, **SAMD** (Zero), and **RP2040**.
- **HAL Refinement**: Continuous improvement of the Hardware Abstraction Layer to support advanced power management and specialized serial controllers.

### 3. Observability & Tooling
- **MCU Logging Utility**: Implementation of `hal::log_hexdump` to allow the MCU to report internal state and parsing errors via the Linux syslog without breaking binary protocol sync.
- **Documentation Expansion**: Comprehensive user guides, API references, and SIL-2 compliance documentation for third-party integrators.
- **Web Interface (LuCI)**: Update the LuCI app to support the new real-time metrics and task supervision status provided by the v2 daemon.
