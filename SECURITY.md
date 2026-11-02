# Security Policy

## Supported Versions

Currently, only the latest major version of Arduino MCU Bridge 2 is actively supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 2.8.x   | :white_check_mark: |
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

Arduino MCU Bridge 2 implements a Safety-Hardened Embedded Architecture designed for high-reliability embedded environments using deterministic FSMs, Zero-Heap memory management, and boot-time Known-Answer Tests.

### 1. Link Authentication and Encryption
- When `serial_shared_secret` is configured, `LinkSync` authenticates the host with HMAC-SHA256 and the MCU returns a domain-separated HMAC proof. Both tags cover the 16-byte host nonce.
- An empty shared secret explicitly disables link authentication. It is not a fallback for a failed tag, and unauthenticated mode must not be used over an untrusted transport.
- RPC frames are **not encrypted** and have no per-frame replay protection. The 16-byte handshake nonce is not a monotonic frame counter.
- Pigweed `pw_crypto` supplies the MCU SHA-256 API. The current Arduino export has no HMAC/HKDF or AEAD API; the firmware composes the handshake HMAC/HKDF using that SHA-256 API. Python uses `cryptography` for its handshake calculations.
- No project-specific TLS 1.3 0-RTT or session-ticket anti-replay behavior is claimed.

### 2. Integrity
- **CRC32 (IEEE 802.3):** HDLC frames include a 32-bit error-detection checksum. CRC is not cryptographic authentication.
- No Poly1305 tag or cryptographic frame-integrity check is currently applied to RPC frames.

### 3. Flash & Resource Protection
- **RAM-only storage:** The daemon enforces that frequent writes (like cloud payload spooling and file operations) occur in `/tmp` (volatile memory).
- **Strict Boundaries:** Payloads are strictly bounded to `MAX_PAYLOAD_SIZE` (64 bytes) to prevent buffer overflows.

### 4. Cryptographic Self-Validation

- **MCU POST:** Project KATs cover SHA-256 and HMAC-SHA256.
- **Python self-test:** The daemon also checks a ChaCha20-Poly1305 library vector. This is not active link encryption.

- These tests are functional checks, not independent validation or certification. A successful KAT does not protect RPC frames from modification or replay.



### 5. Determinism (C++)

- **No STL / No Dynamic Memory:** The Arduino library uses the Embedded Template Library (ETL) with `etl::array` static allocation and `etl::fsm` to ensure deterministic behavior and prevent heap fragmentation.
