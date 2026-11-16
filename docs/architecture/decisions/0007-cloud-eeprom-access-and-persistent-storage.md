# ADR-0007: Cloud Access to the AVR EEPROM and Persistent Linux Storage

- Status: Accepted
- Date: 2026-11-16
- Decision owners: Repository maintainer
- Amends: [ADR-0004](0004-linux-mpu-storage-ssot.md)

## Context

[ADR-0004](0004-linux-mpu-storage-ssot.md) made the OpenWrt Linux filesystem the single source of truth for common storage and kept the ATmega2560 EEPROM (`pw_transfer` resource `0x4D454741`) as a local hardware resource. It left two questions open:

- **D1: Can the cloud reach the AVR EEPROM?** Today the sketch can, through `pw_transfer`, but the daemon never calls its MCU transfer client (`SerialTransport.transfer_read/transfer_write`). The gateway registers a resource with the same ID, but it is a 4096-byte buffer in gateway RAM that is not connected to any MCU.
- **D2: Must Linux storage survive a reboot?** `file_system_root` defaults to `/tmp/yun_files`, which is RAM on OpenWrt (`/var` is also RAM). The "source of truth" of ADR-0004 is therefore lost on every reboot.

Making the EEPROM writable from the cloud also makes the current gateway authorization model insufficient: any authenticated client can address any device, and an empty `topic_authorization` allows everything.

## Decision

1. **D1: the AVR EEPROM is reachable from the cloud.**
   - Path: cloud client → gateway (`EepromService.Read` / `EepromService.Write`) → daemon → MCU `pw_transfer` resource `0x4D454741`.
   - The gateway resource `0x4D454741` becomes the per-device staging area for the EEPROM image. Its size equals the MCU resource: 4,080 bytes (the last 16 bytes hold the ADR-0005 boot epoch and are never exposed).
   - The daemon validates the size, performs one transfer at a time (the existing transfer lock), and reports progress and failures explicitly.
   - Writes keep the one-byte transfer window that protects the UART receive path while the EEPROM controller programs a byte. A full 4,080-byte write takes on the order of tens of seconds; it is measured in simavr before release.
2. **Authorization is deny-by-default.**
   - New authorization fields `eeprom_read` and `eeprom_write`, off by default.
   - The daemon enforces `topic_authorization` on cloud-dispatched operations (`handle_cloud_edge_dispatch`).
   - The gateway checks, per client identity, which devices and operations it may use; an unlisted pair is rejected.
3. **D2: Linux storage is persistent by default.**
   - `file_system_root` defaults to `/srv/mcubridge/files` (OpenWrt overlay). It is no longer marked volatile.
   - `allow_non_tmp_paths` stays the explicit opt-in for other locations; the default persistent root is accepted without it.
   - An existing installation that still has the old default `/tmp/yun_files` is migrated by `uci-defaults`; an explicit user value is kept.
   - The overlay is router flash: the default quota drops from 8 MiB to 2 MiB, and LuCI offers a mounted SD card as the root and warns about flash wear.

## Alternatives considered

- **Keep the EEPROM local only (D1 option A):** simpler and safer, but rejected by the maintainer; projects need to read and provision EEPROM contents remotely.
- **Expose the EEPROM through the generic `FileTransferService`:** rejected; the EEPROM is a fixed-size device resource, not a file, and it needs its own permissions and size checks.
- **Keep Linux storage volatile (D2 option B):** rejected; it contradicts ADR-0004.
- **`/var` or `/tmp` with periodic sync to flash:** rejected; data written between syncs is lost and the sync still wears flash.

## Consequences

- **Breaking changes:**
  - deployments that relied on "everything allowed" must list their permissions;
  - the gateway contract gains `EepromService`;
  - the default storage location and quota change (migrated automatically).
- **Security:** remote writes can alter sketch data in EEPROM. They require `eeprom_write` and the gateway ACL; the boot epoch is outside the resource and cannot be overwritten.
- **Flash wear on the router:** mitigated by the smaller quota, the SD option and the LuCI warning.

## Implementation plan

| Stage | Content |
|---|---|
| E1 | This ADR, ADR-0004 update, ADR renumbering |
| E2 | Deny-by-default authorization and gateway ACL |
| E3 | Persistent `file_system_root`, migration, quota, LuCI |
| E4 | Gateway staging hardening; `0x4D454741` staging sized 4,080 bytes per device |
| E5 | `EepromService` cloud ↔ MCU path, end-to-end test in simavr, write-time measurement |
| E6 | MCU → Linux uploads through `pw_transfer` on AVR, replacing `FileSystemService.Write` |
| E7 | `pw_transfer` on ESP32 (FreeRTOS `pw_thread` backend) |
| E8 | `pw_transfer` on SAMD21 (cooperative mode) |
| E9 | Remove `FileSystemService.Write` and the unused MCU SD-card hooks |

## Related

- [ADR-0004](0004-linux-mpu-storage-ssot.md): storage source of truth.
- [ADR-0005](0005-mcu-contributed-handshake-nonce.md): EEPROM tail reserved for the boot epoch.
- [`docs/architecture/pw-transfer-integration.md`](../pw-transfer-integration.md).
