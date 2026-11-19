# ADR-0008: Retire Unary FileSystemService.Write in Favor of pw_transfer

- Status: Accepted
- Date: 2026-11-19
- Decision owners: Repository maintainer
- Amends: [ADR-0004](0004-linux-mpu-storage-ssot.md), [ADR-0007](0007-cloud-eeprom-access-and-persistent-storage.md) (completes Stage E9)

## Context

[ADR-0004](0004-linux-mpu-storage-ssot.md) established the OpenWrt Linux MPU filesystem (`file_system_root`) as the single source of truth for common persistent storage and removed legacy unary `FileRead` / `FileWrite` messages from the cloud northbound contract. [ADR-0007](0007-cloud-eeprom-access-and-persistent-storage.md) (Stages E6–E8) subsequently delivered upstream Pigweed `pw_transfer` across all three supported MCU targets (`arduino:avr:mega`, `arduino:samd:mkrwifi1010`, and `esp32:esp32:nano_nora`) and introduced `FileSystem.upload(path, data)` backed by `rpc.pb.FileSystemService/Upload` and `FILE_UPLOAD_RESOURCE_ID` (`0x55504C44`), verified end-to-end across all three architectures in Fleet Mesh.

Even after `FileSystem.upload()` replaced small MCU-to-Linux writes on every supported architecture, several transitional artifacts remained:

1. `rpc Write(rpc.pb.FileWrite) returns (rpc.pb.Empty)` and `message FileWrite` (with 64-byte static Nanopb buffers) in `tools/protocol/mcu_wire.proto` and `tools/protocol/mcu_wire.options`.
2. `FileSystemClass::write(...)`, `FileSystemClass::_onWrite(...)`, and `FileSystemRpcService::Write(...)` in `mcubridge-library-arduino`.
3. Unused MCU SD-card HAL hooks (`bridge::hal::writeFile`, `bridge::hal::readFileChunk`, and `bridge::hal::ChunkResult`) in `src/hal/hal.h` and `src/hal/hal.cpp` (the last caller of `readFileChunk` was removed with `FileSystemService.Read`, and `_onWrite` was the sole remaining caller of `writeFile`).
4. `"rpc.pb.FileSystemService/Write": "file_write"` in `BridgeService.LOCAL_RPC_HANDLERS` and dual-mode MCU/cloud/UBUS argument branching in `BridgeService.file_write`.

Retaining a 64-byte transitional unary write RPC alongside `pw_transfer` creates duplicate write code paths, consumes Nanopb descriptor and Pigweed RPC method-table memory on AVR, and keeps dead SD-card stubs alive in the HAL.

## Decision

1. **Retire `rpc.pb.FileSystemService/Write` and `message FileWrite` (clean break):**
   - Remove `rpc Write(rpc.pb.FileWrite) returns (rpc.pb.Empty)` and `message FileWrite` from `tools/protocol/mcu_wire.proto`, and remove `rpc.pb.FileWrite.*` entries from `tools/protocol/mcu_wire.options`.
   - Do not add Protobuf `reserved` tombstones (per Rule 8 of [`docs/architecture/pigweed_migration_plan.md`](../pigweed_migration_plan.md), enforced by `test_proto_integrity.py`).
   - Regenerate all Python and C++/Nanopb protocol bindings from the canonical `.proto` sources via `tools/protocol/generate.py`.

2. **Unify MCU → Linux file writes on `pw_transfer`:**
   - Remove `FileSystemClass::write(...)`, `FileSystemClass::_onWrite(...)`, and `FileSystemRpcService::Write(...)` from `mcubridge-library-arduino`.
   - Sketches write files to the MPU filesystem exclusively via `FileSystem.upload(path, data)` (`rpc.pb.FileSystemService/Upload` + `pw_transfer` resource `0x55504C44`), subject to path sandboxing, `file_write_max_bytes`, and `file_storage_quota_bytes`.
   - Remove `"rpc.pb.FileSystemService/Write"` from `BridgeService.LOCAL_RPC_HANDLERS`. Any inbound MCU packet targeting the retired `(FileSystemService, Write)` hashes is rejected with `Status.UNIMPLEMENTED` on its `call_id` (and increments `unknown_command_count`); cloud requests targeting `req/rpc.pb.FileSystemService/Write` are rejected as unrecognized RPC methods.

3. **Remove unused MCU SD-card HAL hooks (ADR-0007 Stage E9):**
   - Remove `bridge::hal::ChunkResult`, `bridge::hal::writeFile(...)`, and `bridge::hal::readFileChunk(...)` from `mcubridge-library-arduino/src/hal/hal.h` and `src/hal/hal.cpp`.
   - Retain `bridge::hal::hasSD()` (used by `fillCapabilities()` for `Capabilities.sd`) and `bridge::hal::removeFile(...)` (used by `FileSystemClass::_onRemove()` for `FileSystemService.Remove`).

4. **Explicitly retain `Bridge.transferClient()`:**
   - `Bridge.transferClient()` remains part of the public `BridgeClass` API alongside `Bridge.transferService()`, sharing the single cooperative `pw::transfer::Thread<1, 1>`.
   - `mcubridge-library-arduino/tests/pw_transfer_compile/pw_transfer_compile.ino` exercises `Bridge.transferClient().Read(...)` as a mandatory CI compilation gate, and retaining the client preserves symmetric MCU-initiated `pw_transfer` transfers without adding another thread.

5. **Preserve authorization controls, `FileSystemService.Remove`, and OpenWrt UBUS `mcubridge.file_write`:**
   - Retain `TopicAuthorization.file_write` (`cloud_allow_file_write` in UCI/LuCI) and `file_write_max_bytes`: they continue to authorize and bound `rpc.pb.FileSystemService/Upload` and `rpc.pb.FileTransferService/PushToLinux`.
   - Retain `rpc.pb.FileSystemService/Remove` (`auth_field = "file_remove"`) for unlinking files inside `file_system_root`.
   - Retain the MPU-local OpenWrt UBUS IPC method `mcubridge.file_write` (`UbusService.ubus_handle_file_write` → `BridgeService.file_write(path: str, data: bytes)`), used by local OpenWrt tools (`mcubridge-file-push`). `BridgeService.file_write` is simplified to accept strictly `(str, bytes)` for local MPU writes and is no longer registered in `LOCAL_RPC_HANDLERS` or callable via MCU/Cloud Pigweed RPC.

## Alternatives considered

- **Keep `FileSystemService.Write` for small (≤ 64-byte) files:** Rejected. Maintaining two MCU-to-Linux write protocols duplicates validation, tests, and firmware RPC dispatch tables for a narrow payload cap that `FileSystem.upload()` already supersedes across all three architectures.
- **Remove `Bridge.transferClient()` alongside `FileSystemService.Write`:** Rejected. `pw_transfer_compile.ino` explicitly validates `Bridge.transferClient().Read(...)` as a CI gate, and sketches rely on the symmetric `pw_transfer` client/service API over the shared `Thread<1, 1>`.
- **Also remove `FileSystemService.Remove` or OpenWrt UBUS `mcubridge.file_write`:** Rejected. `pw_transfer` handles byte-stream transfer rather than file deletion, so `FileSystemService.Remove` remains a real administrative operation (`fileio_test.py` exercises it). OpenWrt UBUS `mcubridge.file_write` is a local MPU IPC contract used by `mcubridge-file-push` and LuCI ACLs, independent of the MCU wire protocol.
- **Keep `reserved` field/message tombstones in `mcu_wire.proto`:** Rejected. Repository policy (Rule 8) mandates coordinated schema updates without tombstones, verified by `test_proto_integrity.py`.

## Consequences

- **Positive:**
  - Single canonical MCU ↔ MPU file-content transfer mechanism (`pw_transfer` via `FileSystem.upload()` and AVR EEPROM resource `0x4D454741`).
  - Eliminates Nanopb `rpc_pb_FileWrite` descriptors, `FileSystemService::Write` server/client stubs, `FileSystemClass::write`/`_onWrite`, and dead weak HAL SD-card stubs (`writeFile`, `readFileChunk`), reducing Flash and static method-table footprint across AVR, SAMD21, and ESP32-S3.
  - Simplifies `BridgeService.file_write` to a 3-line local MPU storage helper for UBUS with strict `(str, bytes)` typing.
- **Breaking changes:**
  - MCU sketches calling `FileSystem.write(...)` fail to compile and must migrate to `FileSystem.upload(path, data)` + `FileSystem.uploadPending()` / `FileSystem.lastUploadStatus()`.
  - Cloud clients attempting to dispatch `req/rpc.pb.FileSystemService/Write` are ignored/rejected and must use `FileTransferService/PushToLinux` (via `GatewayClient.push_file_to_device` / `push_bytes_to_device`).
  - Custom HAL overrides implementing `bridge::hal::writeFile` or `bridge::hal::readFileChunk` are no longer invoked by the library.
- **Resource measurements:**
  - Pre-change baseline (`BridgeControl.ino` on `ca387194`): AVR Mega 2560 `121,196` B Flash / `5,813` B SRAM; SAMD21 MKR WiFi 1010 `104,656` B Flash / `10,528` B SRAM; ESP32-S3 Nano ESP32 `384,141` B Flash / `27,672` B SRAM. Post-change per-target Flash/SRAM deltas are verified via `tools/ci/compile_firmware.sh` (`resource_report.json`) in CI across all three architectures.

## Affected Contracts

- `tools/protocol/mcu_wire.proto` & `tools/protocol/mcu_wire.options` (and all generated bindings in `mcubridge/mcubridge/protocol/`, `mcubridge-library-arduino/src/protocol/`, `mcubridge-client-examples/mcubridge_client/`).
- `mcubridge-library-arduino/src/services/FileSystem.{h,cpp}`, `src/Bridge.cpp`, `src/hal/hal.{h,cpp}`.
- `mcubridge/mcubridge/services/runtime.py` (`LOCAL_RPC_HANDLERS`, `BridgeService.file_write`).
- Host C++ tests, Python unit/contract tests, and architecture/protocol documentation.

## Migration and Rollback

- **Migration:** Upgrade firmware and MPU daemon together in the same maintenance window. Update any custom Arduino sketches from `FileSystem.write(path, data)` to `FileSystem.upload(path, data)` (keeping the source buffer valid until `FileSystem.uploadPending()` becomes `false`). No UCI/LuCI permission changes are required: `cloud_allow_file_write` continues to govern `FileSystemService/Upload` and `FileTransferService/PushToLinux`.
- **Mixed-version behavior:** Older firmware sending `FileSystemService.Write` to an updated daemon receives `pw::Status::UNIMPLEMENTED` cleanly on its RPC `call_id` (no crash or framing desync). Updated firmware calling `FileSystem.upload()` requires a daemon with ADR-0007 Stage E6 (`FileSystemService/Upload`).
- **Rollback:** Revert the coordinated protocol/firmware/daemon commit, regenerate protocol bindings, and deploy matching firmware and daemon builds together.

## Related

- [ADR-0002](0002-pigweed-hdlc-rpc-migration.md): Pigweed HDLC/RPC migration.
- [ADR-0004](0004-linux-mpu-storage-ssot.md): Linux MPU storage SSOT.
- [ADR-0007](0007-cloud-eeprom-access-and-persistent-storage.md): Cloud EEPROM access, persistent Linux storage, and Stages E6–E9.
- [`docs/architecture/pw-transfer-integration.md`](../pw-transfer-integration.md): Upstream `pw_transfer` ownership and transfer boundaries.
