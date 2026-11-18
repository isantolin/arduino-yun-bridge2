# MCU Bridge — Release Notes (next, unreleased)

**Status:** Draft. It covers the changes merged after 2.8.9: PR #86 (E0) and PRs #87, #88, #89 (E0.1 B, C, D). PR #90 (E0.1 E) and PR #92 (E0.1 F, handshake v2) are merged. Publish only after the release CI, emulation and packaging checks pass on the release commit.

## ⚠️ Breaking changes and migration

### 1. `allowed_commands` matching is exact and case-sensitive (PR #87)

Before, entries were lowercased and matched with `fnmatch` against the first whitespace token, while execution used `shlex`. Now the command is parsed once with `shlex`, and the same `argv` is checked and executed.

| Entry | Before | Now |
|---|---|---|
| `ls` | allowed `ls`, `LS`, `Ls` | allows only `ls` |
| `ls` | did not allow `/bin/ls` | still does not; add `/bin/ls` explicitly |
| `/usr/bin/*` | matched any path under it, including `/usr/bin/../../tmp/x` | literal path only; globs never match paths |
| `py*` | matched `python3` and `/usr/bin/python3` | matches `python3`, not paths |
| `*` | allow all | allow all (unchanged) |

**Migration:** review `uci get mcubridge.general.allowed_commands`. Write program names with their real case, and full executable paths as exact paths.

### 2. An empty `serial_shared_secret` is rejected unless explicitly allowed (PR #88)

An empty secret runs the MCU link unauthenticated and unencrypted. The daemon now refuses to start with an empty secret unless `allow_insecure_link=1`. When it is set, the cloud transport is disabled and an `INSECURE LINK` error is logged.

**Migration:** installations keep the random secret generated at install time, so no action is needed. If you deliberately run without a secret, set `uci set mcubridge.general.allow_insecure_link=1` (LuCI: *Allow insecure serial link*) and expect the cloud to be off.

### 3. Link handshake v2: upgrade firmware and daemon together (PR #92)

The MCU now contributes its own 16-byte nonce to `LinkSync` (`mcu_nonce`), and both the response tag and the session keys depend on it (ADR-0005). A v1 firmware and a v2 daemon, or the reverse, fail closed with `sync_auth_mismatch`; they never fall back to a weaker link.

Side effects:
- On AVR the last 16 bytes of the EEPROM hold the boot epoch, so the `pw_transfer` EEPROM resource shrinks from 4096 to 4080 bytes.
- SAMD21 builds need the `FlashStorage` 1.0.0 library (installed by `install.sh` and the Library Manager `depends`).

**Migration:** flash the new firmware and upgrade the daemon in the same maintenance window. After re-flashing a SAMD21 sketch or an ISP chip erase of an AVR, rotate `serial_shared_secret`, because the boot epoch restarts.

### 4. Cloud authorization is deny-by-default (ADR-0007)

- **Daemon:** before, when no `cloud_allow_*` flag was set, every cloud operation was allowed. Now nothing is allowed until it is granted, and LuCI shows every permission off by default. New permissions `cloud_allow_eeprom_read` and `cloud_allow_eeprom_write`.
- **Gateway:** `GatewayControl.Dispatch` requires an ACL file (`--acl`) that lists, for each client, the devices and operations it may use. Without it every dispatch is denied.

**Migration:** enable in LuCI (or `uci set mcubridge.general.cloud_allow_<name>=1`) only the permissions your clients use, and start the gateway with `--acl` (format in `mcubridge-gateway-cpp/README.md`).

### 5. File storage is persistent by default (ADR-0007)

`file_system_root` moves from `/tmp/yun_files` (RAM, lost on reboot) to `/srv/mcubridge/files` (router overlay), which is kept across `sysupgrade`. The default `file_storage_quota_bytes` drops from 8 MiB to 2 MiB to limit flash wear. Roots under `/srv/mcubridge`, `/mnt` (SD card) or RAM are accepted without `allow_non_tmp_paths`.

**Migration:** automatic. An installation still using `/tmp/yun_files` is moved to the new default on upgrade; an explicit root you chose is kept. Existing files under `/tmp/yun_files` are not copied (they did not survive reboots anyway). For frequent writes, point `file_system_root` to an SD card under `/mnt`.

### 6. AVR builds in CI use the core's default serial buffers (PR #86)

CI no longer forces `SERIAL_RX/TX_BUFFER_SIZE=256` for AVR. Sketches built by users never had it; this aligns CI with them. No user action is needed.

## Fixes

- **Gateway device transfers (PR #86).** Device sessions registered their `pw_transfer` handlers before starting the transfer thread, so every device-side transfer failed with `NOT_FOUND`. Registration failures are now logged.
- **ATmega2560 stack overflow (PR #86).** The firmware stack overflowed into `.bss` and corrupted the `HardwareSerial` object, causing hangs and crashes under load. The stack peak dropped from 1789 B to about 1514 B against a 1791 B budget:
  - wolfSSL keeps the SHA-256 table in flash (`WOLFSSL_USE_FLASHMEM`) and uses a 16-word schedule (`SHA256_MANY_REGISTERS`);
  - encrypted frames are streamed in 32-byte chunks instead of a 256-byte stack envelope (identical bytes on the wire);
  - crypto self-tests run in separate frames.
- **Process subsystem (PR #87):**
  - malformed quoting no longer leaks a process slot;
  - process handles are the real PID instead of `pid & 0xFFFF`.
- **Mailbox (PR #89).** Cloud messages are delivered to the MCU once. They were pushed *and* queued, and then read again by the sketch's polling. The examples poll only every 5 s as a fallback.
- **Serial flow control (PR #90).** XOFF/XON thresholds now follow each core's RX buffer: AVR 48/16 B, SAMD/ESP32 192/64 B. This removes needless XOFF/XON bursts on 256-byte-buffer boards.

## Features

- **Cloud access to the AVR EEPROM (ADR-0007).** `EepromService.Read/Write` copies the EEPROM image (up to 4,080 bytes) between the gateway and the MCU. The client library adds `read_mcu_eeprom()` and `write_mcu_eeprom()`. It requires the `cloud_allow_eeprom_read` / `cloud_allow_eeprom_write` permissions and a gateway ACL rule; writes are not transactional.
- `pw_transfer` is enabled by default on AVR. Build with `-DBRIDGE_ENABLE_PW_TRANSFER=0` (global build flag) to reclaim about 0.5 KB of SRAM.
- simavr diagnostics (`avr_pigweed_port` #17/#18):
  - the runner reports the PC, SP and stack low-water mark on a crash;
  - Fleet Mesh symbolizes the call chain and aborts as soon as an emulator exits.

## Compatibility

- **MCU ↔ MPU wire protocol:** `LinkSync` gains `mcu_nonce` (field 3) and the handshake HKDF labels move to v2 (PR #92). The streamed AEAD frame format is unchanged.
- **Configuration schema:** new `allow_insecure_link` field (RuntimeConfig 54) and per-architecture flow-control constants (Hardware 23/24/46/47).

## Known limitations

- `LinkReset` is still accepted in plaintext, so an attacker on the transport can force resynchronisation (denial of service). Replaying a recorded session is no longer possible (ADR-0005).
- Re-flashing a SAMD21 sketch or an ISP chip erase of an AVR resets the boot epoch behind the MCU nonce; rotate `serial_shared_secret` afterwards.
- Any authenticated gateway client can address any device. With `topic_authorization` unset, all operations are allowed.
