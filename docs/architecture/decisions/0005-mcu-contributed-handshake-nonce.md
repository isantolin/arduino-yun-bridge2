# ADR-0005: MCU-Contributed Nonce in the Link Handshake

- Status: Proposed
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

## Decision (proposed)

1. **Wire format.**
   - Add `bytes mcu_nonce = 3` (16 bytes) to `rpc.pb.LinkSync`; the MCU fills it in its response.
   - Bump the handshake to v2 with new HKDF `info` labels for the response tag and the session keys, so a v1 peer fails closed with an authentication mismatch instead of deriving silently different keys.
2. **Response tag:** `HMAC(K_resp_v2, nonce_h ‖ mcu_nonce)[:16]`. The host verifies the MCU's knowledge of the secret bound to both nonces.
3. **Session keys:** `HKDF-SHA256(secret, salt = nonce_h ‖ mcu_nonce, info = SESSION_v2)`, still split host→MCU / MCU→host.
4. **`mcu_nonce` generation**, per platform:
   - **ESP32:** `esp_fill_random()`.
   - **AVR:** `HMAC-SHA256(K_nonce, boot_counter ‖ handshake_counter)[:16]`, where `K_nonce = HKDF(secret, info = "mcubridge nonce v2")`.
     - `boot_counter` is a 64-bit counter in a **reserved EEPROM slot**, incremented once per boot with `EEPROM.update`.
     - `handshake_counter` is a RAM counter incremented per `LinkSync`.
     - Uniqueness comes from the counters; the HMAC only hides them.
   - **SAMD21:** same construction as AVR, with `boot_counter` in flash. Option (a) below is recommended.
5. **Documentation:** correct `PROTOCOL.md` and the FIPS policy text. Replay of a recorded session is prevented. An unauthenticated `LinkReset` remains a denial-of-service vector, tracked separately.

## Decisions requested from the maintainer

- **D5a: AVR EEPROM reservation.** Reserving 16 bytes at the end of the EEPROM shrinks the `pw_transfer` EEPROM resource from 4096 to 4080 bytes on the Mega. That is a contract change, interacting with D1 (whether the EEPROM is reachable at all), and `pw_transfer_compile.ino` asserts 4096 today.
- **D5b: SAMD21 boot counter source.**
  - **(a) Recommended:** emulated EEPROM in flash via the `FlashStorage` library. This is a **new dependency** that requires approval. One row write per boot. The SAMD21 NVM endurance per row must be confirmed against the datasheet before acceptance; wear-levelling across pages is available if needed.
  - **(b)** The ATECC608 TRNG present on the MKR WiFi 1010 (`ArduinoECCX08`). Also a new dependency, and board-specific.
  - **(c)** Drop SAMD21 support for authenticated links. Not recommended.

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

## Validation plan

- **Both sides:**
  - known-answer tests for the v2 tag and key derivation;
  - a regression test that replays a recorded `LinkSync` after `LinkReset` and must obtain different keys, rejecting the recorded frames;
  - a v1/v2 mismatch test that fails closed.
- **Hardware emulation:** Fleet Mesh on all three boards, with the AVR stack peak reported.

## Related

- Supersedes the replay limitation note in `docs/PROTOCOL.md` ("anti-replay solo dura lo que la sesión").
- Related: ADR-0002 (Pigweed HDLC/RPC), ADR-0003 (wolfCrypt on ESP32), D1 (EEPROM reachability).
