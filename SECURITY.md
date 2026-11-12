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
- With a non-empty `serial_shared_secret`, `LinkSync` authenticates the host with HMAC-SHA256 and the MCU returns a domain-separated HMAC proof. Both tags cover the 16-byte host nonce.
- An empty `serial_shared_secret` explicitly selects the compatibility mode without link authentication or HDLC payload encryption. This mode must be limited to isolated development/testing links; it must not be used on an untrusted physical or network path.
- The handshake derives separate 256-bit host-to-MCU and MCU-to-host keys with HKDF-SHA256. After synchronization, RPC, console, and log HDLC payloads use ChaCha20-Poly1305. The envelope is an 8-byte big-endian counter followed by ciphertext and a 16-byte tag; the HDLC address and counter are authenticated as AAD.
- Each direction uses one strictly increasing counter across the protected HDLC channels. Receivers reject zero, repeated, and older counters. This replay defense is scoped to the current session; because the MCU has no demonstrated cross-platform cryptographic random source or persistent boot epoch, previously captured handshakes and frames are not guaranteed to be rejected after an MCU restart.
- `LinkReset` is an explicit plaintext control operation used to recover synchronization. This is not a plaintext fallback for data frames; a reset can force a new handshake.
- An empty shared secret explicitly disables link authentication. It is not a fallback for a failed tag, and unauthenticated mode must not be used over an untrusted transport.
- Pigweed remains responsible for MCU HDLC/RPC framing. Mbed TLS provides the MCU's ChaCha20-Poly1305, HKDF, HMAC, SHA-256, constant-time comparison, and secure-zeroization APIs; Python uses `cryptography`.
- No project-specific TLS 1.3 0-RTT or session-ticket anti-replay behavior is claimed.

### 2. Integrity
- **CRC32 (IEEE 802.3):** HDLC frames include a 32-bit error-detection checksum. CRC is not cryptographic authentication.
- With a configured shared secret, every post-handshake RPC, console, and log payload is authenticated with a Poly1305 tag. The handshake response and explicit `LinkReset` control request are not AEAD-protected.

### 3. Flash & Resource Protection
- **RAM-only storage:** The daemon enforces that frequent writes (like cloud payload spooling and file operations) occur in `/tmp` (volatile memory).
- **Strict Boundaries:** Payloads are strictly bounded to `MAX_PAYLOAD_SIZE` (64 bytes) to prevent buffer overflows.

### 4. Cryptographic Self-Validation

- **MCU POST:** Project KATs cover SHA-256, HMAC-SHA256, and ChaCha20-Poly1305.
- **Python self-test:** The daemon checks a ChaCha20-Poly1305 library vector. Link-frame encryption is also exercised by transport tests.

- These tests are functional checks, not independent validation or certification. They do not establish protection against replay across MCU restarts.



### 5. Determinism (C++)

- **No STL / No Dynamic Memory:** The Arduino library uses the Embedded Template Library (ETL) with `etl::array` static allocation and `etl::fsm` to ensure deterministic behavior and prevent heap fragmentation.
