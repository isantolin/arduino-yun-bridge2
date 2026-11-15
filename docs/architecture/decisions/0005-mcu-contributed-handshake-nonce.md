# ADR-0005: MCU-Contributed Nonce in the Link Handshake

- Status: Accepted (implemented in PR #92)
- Date: 2026-11-15
- Decision owners: Repository maintainer

## Context

The MCU ↔ MPU serial link authenticates with `rpc.pb.SystemService/LinkSync` and then protects every HDLC frame with ChaCha20-Poly1305 (`docs/PROTOCOL.md`).

Today only the host contributes freshness:

- The host sends `LinkSync{nonce_h, tag = HMAC(K_auth, nonce_h)[:16]}`.
- The MCU answers `LinkSync{nonce_h, tag = HMAC(K_resp, nonce_h)[:16]}`.
- Both sides derive the session keys as `HKDF-SHA256(secret, salt = nonce_h, info = SESSION)` and reset both AEAD counters to 0 (`security.cpp:derive_session_keys`, `security.py:derive_link_keys`, `Bridge.cpp:_handleLinkSync`).
- The MCU accepts a plaintext, unauthenticated `rpc.pb.SystemService/LinkReset` at any time, even with an active session (`Bridge.cpp:_handleReceivedHdlcFrame`).

An attacker with access to the serial transport can therefore record a session, send `LinkReset`, and replay the recorded host `LinkSync`. The MCU accepts the tag, derives **the same session keys**, and resets its counters. Two consequences follow:

1. **Command replay.** Every recorded host→MCU frame (GPIO writes, process requests, …) decrypts and passes the counter check again.
2. **Keystream reuse.** The MCU encrypts new traffic under the same key and the same counter-derived nonces as the recorded session, which exposes plaintext XORs and the Poly1305 one-time keys for those nonces.

No MCU reboot is needed. `docs/PROTOCOL.md` and `docs/security/FIPS140_3_SECURITY_POLICY.md` currently limit the exposure to "after an MCU restart", which understates it. The transports are not limited to an internal UART: the Mega, MKR and Nano ESP32 setups are separate boards, and the examples include Bluetooth and Wi-Fi streams.

The fix is for each session key to depend on a value the MCU itself makes unique. That value must **never repeat for the same secret**, across reboots too. It does not have to be secret, because the keys also depend on the shared secret.

Verified per-platform sources (installed cores):

| Platform | Hardware RNG | Persistent storage |
|---|---|---|
| ESP32 (Nano ESP32, core 3.3.12) | Yes: `esp_fill_random()` (`esp_random.h`) | NVS (not needed) |
| SAMD21 (MKR WiFi 1010) | **No** (TRNG exists only on SAMD51) | No EEPROM; flash only |
| AVR (Mega 2560) | **No** | 4 KiB EEPROM, today fully exposed as `pw_transfer` resource `0x4D454741` |

## Decision

1. **Wire format.**
   - Add `bytes mcu_nonce = 3` (16 bytes) to `rpc.pb.LinkSync`; the MCU fills it in its response.
   - Bump the handshake to v2 with new HKDF `info` labels, so a v1 peer fails closed with an authentication mismatch instead of deriving silently different keys: `session-key-v2`, `handshake-response-v2` and `mcu-nonce-v2` (`config_schema.proto`, generated as `RPC_HANDSHAKE_HKDF_INFO_*` / `HANDSHAKE_HKDF_INFO_*`).
   - The host rejects a response without a 16-byte `mcu_nonce` as `sync_auth_mismatch`.
2. **Response tag:** `HMAC(K_resp_v2, nonce_h ‖ mcu_nonce)[:16]`. The host verifies the MCU's knowledge of the secret bound to both nonces.
3. **Session keys:** `HKDF-SHA256(secret, salt = nonce_h ‖ mcu_nonce, info = SESSION_v2)`, still split host→MCU / MCU→host.
4. **`mcu_nonce` generation**, per platform:
   - **ESP32:** `esp_fill_random()`.
   - **AVR and SAMD21:** `HMAC-SHA256(K_nonce, boot_epoch ‖ handshake_counter)[:16]`, where `K_nonce = HKDF(secret, info = "mcu-nonce-v2")` and both counters are 64-bit big-endian (`bridge::hal::fillLinkNonce`, `rpc::security::derive_mcu_nonce`).
     - `boot_epoch` is persisted as **two 8-byte copies**. Each boot reads both (erased storage counts as 0), takes the larger value plus one, and overwrites the **older** copy before the first nonce is emitted. A write torn by a power loss leaves the other copy intact, so the epoch still moves forward on the next boot.
     - AVR stores the copies in the last 16 bytes of the EEPROM (D5a); SAMD21 stores each copy in its own NVM row through `FlashStorage` (D5b).
     - `handshake_counter` is a RAM counter incremented per `LinkSync`.
     - Uniqueness comes from the counters; the HMAC only hides them.
   - A POST known-answer test covers the derivation (`kat_mcu_nonce_from_zero_counter`).
5. **Documentation:** correct `PROTOCOL.md` and the FIPS policy text. Replay of a recorded session is prevented. An unauthenticated `LinkReset` remains a denial-of-service vector, tracked separately.

## Maintainer decisions

- **D5a: AVR EEPROM reservation — accepted.** The last 16 bytes of the EEPROM are reserved (`LINK_EPOCH_EEPROM_RESERVED_BYTES`). The `pw_transfer` EEPROM resource `0x4D454741` shrinks from 4096 to 4080 bytes on the Mega; `pw_transfer_compile.ino` asserts the new size. This interacts with D1 (whether the EEPROM is reachable at all).
- **D5b: SAMD21 boot counter source — option (a) accepted.** `FlashStorage` 1.0.0 (`cmaglie/FlashStorage`, LGPL-2.1) is approved as a new dependency, pinned by archive SHA-256 in `requirements/runtime.toml` and generated into `install.sh`. Rejected options:
  - **(b)** The ATECC608 TRNG on the MKR WiFi 1010 (`ArduinoECCX08`): also a new dependency, and board-specific.
  - **(c)** Dropping SAMD21 support for authenticated links.

## Alternatives considered

- **Remember recently accepted host nonces in RAM.** This blocks replays only until the next MCU reboot, which an attacker can often force. Rejected as insufficient on its own.
- **Authenticate `LinkReset`.** This prevents the forced reset but not the replay after a genuine reboot. Useful later for denial-of-service resistance, but it does not fix key reuse.
- **SRAM power-up noise or timer jitter as the only AVR/SAMD21 entropy.** Not guaranteed unique across warm resets. May be mixed in, but not relied upon.
- **Host-side defence only.** The replayed frames target the MCU, so the MCU must reject them.

## Consequences

- **Breaking wire change:** the daemon and the firmware must be upgraded together. A v1/v2 mix fails closed at `LinkSync` with an explicit authentication error.
- **Affected components:**
  - `tools/protocol/mcu_wire.proto` and `config_schema.proto` (labels), plus the generated bindings;
  - `security.cpp`/`.h`, `Bridge.cpp`, the HAL nonce source per architecture;
  - `mcubridge/security/security.py` and `services/handshake.py`;
  - the emulator, KATs (both sides), `docs/PROTOCOL.md` and the FIPS policy.
- **AVR stack:** HKDF/HMAC is already the deepest path (1514 / 1791 B measured). The extra HMAC for `mcu_nonce` runs at the same depth and must be measured with the simavr low-water report before merge.
- **Rollback:** revert the firmware and the daemon together. The new fields and labels are additive in the schema.
- **Known limitations:**
  - Re-flashing a SAMD21 sketch rewrites the `FlashStorage` rows with zeros, and an ISP chip erase clears the AVR EEPROM. In both cases the boot epoch restarts and earlier nonces can repeat. Rotate the shared secret after such an operation.
  - Endurance: each copy is written every other boot. With roughly 25k erase cycles per SAMD21 row and 100k per AVR EEPROM cell, that allows about 50k and 200k boots respectively.
  - The Renode SAMD21 model has no NVM controller. The emulation presets `NVMCTRL.INTFLAG.READY`, and flash writes land directly in memory, so the emulator exercises the code path but not real erase semantics.

## Validation plan

- **Both sides:**
  - known-answer tests for the v2 tag and key derivation;
  - a regression test that replays a recorded `LinkSync` after `LinkReset` and must obtain different keys, rejecting the recorded frames;
  - a v1/v2 mismatch test that fails closed.
- **Hardware emulation:** Fleet Mesh on all three boards, with the AVR stack peak reported.

## Related

- Supersedes the replay limitation note in `docs/PROTOCOL.md` ("anti-replay solo dura lo que la sesión").
- Related: ADR-0002 (Pigweed HDLC/RPC), ADR-0003 (wolfCrypt on ESP32), D1 (EEPROM reachability).
