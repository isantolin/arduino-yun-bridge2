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

### 1. Handshake & Encryption
All serial communication requires a successful handshake using a pre-shared secret.
- **Mutual Authentication:** HMAC-SHA256 used to authenticate the link during synchronization.
- **AEAD (ChaCha20-Poly1305):** All functional data is encrypted and authenticated using mission-critical ChaCha20-Poly1305.
- **Anti-Replay:** Monotonic nonces with counter validation prevent replay attacks on both the handshake and functional frames.
- **MIL-SPEC Compliance:** HKDF-SHA256 (RFC 5869) for session key derivation following NIST standards.

### 2. Integrity
- **CRC32 (IEEE 802.3):** Every frame includes a mandatory 32-bit CRC.
- **Poly1305 MAC:** Every encrypted frame includes a 128-bit authentication tag protecting the header and payload.

### 3. Flash & Resource Protection
- **RAM-only storage:** The daemon enforces that frequent writes (like MQTT spooling and file operations) occur in `/tmp` (volatile memory).
- **Strict Boundaries:** Payloads are strictly bounded to `MAX_PAYLOAD_SIZE` (64 bytes) to prevent buffer overflows.

### 4. Cryptographic Self-Validation

- **Power-On Self-Tests (POST):** The system implements Known Answer Tests (KAT) for SHA256, HMAC-SHA256, and ChaCha20-Poly1305 based on NIST and RFC vectors.

- **Fail-Secure:** Initialization aborts if the cryptographic engine fails the startup tests, ensuring no communication happens over an untrustworthy link.



### 5. Determinism (C++)

- **No STL / No Dynamic Memory:** The Arduino library uses the Embedded Template Library (ETL) with static allocation to ensure deterministic behavior and prevent heap fragmentation.
havior and prevent heap fragmentation.
