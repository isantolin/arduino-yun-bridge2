# Security Policy

## Supported Versions

Currently, only the latest major version of Arduino MCU Bridge 2 is actively supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 2.5.x   | :white_check_mark: |
| 2.x.x   | :white_check_mark: |
| 1.x.x   | :x:                |

## Reporting a Vulnerability

**Do not report security vulnerabilities through public GitHub issues.**

If you discover a potential security vulnerability in this project, please report it by sending an email to **ignacio@example.com** (placeholder).

### What to expect
- An acknowledgment of your report within 48 hours.
- A proposed timeline for a fix or disclosure.
- Updates on the progress of the investigation and remediation.

## Security Architecture

Arduino MCU Bridge 2 is built with security and robustness as primary goals, especially for industrial and safety-critical environments (**SIL-2 compliant**).

### 1. Handshake & Authentication
All serial communication requires a successful handshake using a pre-shared secret.
- **HMAC-SHA256:** Used to authenticate the link during synchronization.
- **Anti-Replay:** Nonces with monotonic counters are used to prevent replay attacks on the handshake.
- **MIL-SPEC Compliance:** HKDF key derivation following NIST standards.

### 2. Integrity
- **CRC32 (IEEE 802.3):** Every frame includes a mandatory 32-bit CRC. Frames with corrupted data are discarded immediately at the transport layer before any parsing occurs.

### 3. Flash & Resource Protection
- **RAM-only storage:** The daemon enforces that frequent writes (like MQTT spooling and file operations) occur in `/tmp` (volatile memory) to prevent flash wear and hardware degradation on OpenWrt devices.
- **Strict Boundaries:** Payloads are strictly bounded to `MAX_PAYLOAD_SIZE` (128 bytes) to prevent buffer overflows.

### 4. Determinism (C++)
- **No STL / No Dynamic Memory:** The Arduino library uses the Embedded Template Library (ETL) with static allocation to ensure deterministic behavior and prevent heap fragmentation.