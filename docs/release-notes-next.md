# MCU Bridge — Release Notes (next, unreleased)

**Status:** Draft. It covers the changes merged after 2.8.9: PR #86 (E0) and PRs #87, #88, #89 (E0.1 B, C, D). PR #90 (E0.1 E) is included as pending merge. Publish only after the release CI, emulation and packaging checks pass on the release commit.

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

### 3. AVR builds in CI use the core's default serial buffers (PR #86)

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
- **Serial flow control (PR #90, pending).** XOFF/XON thresholds now follow each core's RX buffer: AVR 48/16 B, SAMD/ESP32 192/64 B. This removes needless XOFF/XON bursts on 256-byte-buffer boards.

## Features

- `pw_transfer` is enabled by default on AVR. Build with `-DBRIDGE_ENABLE_PW_TRANSFER=0` (global build flag) to reclaim about 0.5 KB of SRAM.
- simavr diagnostics (`avr_pigweed_port` #17/#18):
  - the runner reports the PC, SP and stack low-water mark on a crash;
  - Fleet Mesh symbolizes the call chain and aborts as soon as an emulator exits.

## Compatibility

- **MCU ↔ MPU wire protocol:** unchanged in this release; the streamed AEAD frames are byte-identical. ADR-0005 (proposed) will change the handshake in a later release and will require upgrading the firmware and the daemon together.
- **Configuration schema:** new `allow_insecure_link` field (RuntimeConfig 54) and per-architecture flow-control constants (Hardware 23/24/46/47).

## Known limitations

- The link handshake still derives keys from the host nonce only. A recorded session can be replayed after a forced `LinkReset` (see ADR-0005, proposed).
- Any authenticated gateway client can address any device. With `topic_authorization` unset, all operations are allowed.
