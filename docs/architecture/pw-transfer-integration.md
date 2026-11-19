# Upstream pw_transfer integration (Arduino Mega 2560)

McuBridge uses upstream Pigweed `pw_transfer` through the existing `pw_rpc` / HDLC channel as its sole file-transfer architecture. The transfer thread, service, and client are always part of the firmware; there is no build-time or runtime enable/disable switch and no fallback transfer protocol. AVR-specific EEPROM handling is a hardware adapter registered on AVR, while the generic transfer service remains active. The legacy `FileSystemService.Read` Nanopb stream has been removed from the schema, firmware, and host tests.

## Runtime ownership

- `BridgeClass` owns one `pw::transfer::Thread<1, 1>`.
- The AVR thread uses a 128-byte chunk buffer and a 256-byte encode buffer, satisfying upstream's requirement that the encode buffer exceed the chunk buffer and cover the configured frame size.
- The Python protocol generator registers the upstream `pw.transfer.Transfer` descriptor from Pigweed's `pw_transfer.transfer_pb2`, so the daemon RPC client can resolve `Read`, `Write`, and `GetResourceStatus`.
- `SerialTransport` lazily owns one `pw_transfer.Manager` for that RPC channel. Its `transfer_read()` and `transfer_write()` methods run the blocking Manager calls in worker threads and serialize them with one asyncio lock; the Manager's 128-byte chunk cap matches the AVR staging buffer.
- The runtime maps the explicit path `mcu/eeprom` to resource `0x4D454741`. Other `mcu/*` paths are rejected instead of falling back to the removed path-based MCU RPC. Non-`mcu/*` paths continue to use the MPU's local filesystem.
- The same thread is shared by `Bridge.transferService()` and `Bridge.transferClient()`.
- `Bridge.process()` polls the thread with a bounded event budget after receiving serial/HDLC packets.
- Applications must not create another transfer thread or call `Poll()` themselves.
- The port's role arbiter permits one active transfer role at a time. Client and server transfers can be reused sequentially, not simultaneously.
- This integration is enabled only for the ATmega2560 application target; smaller AVR boards are excluded.

The API is available when compiling for Arduino Mega 2560:

```cpp
Bridge.transferService()
Bridge.transferClient()
```

## Registering an application resource

On AVR, the EEPROM resource is registered automatically by `Bridge.begin()` as a hardware-specific `pw_transfer` resource. Its stable resource ID is `0x4D454741` and its resource size is 4,080 bytes (the last 16 bytes of the EEPROM hold the link boot epoch, ADR-0005). Applications can still register additional resources, provided handlers and backing streams have static or otherwise transfer-long lifetime. For example, a read-only in-memory resource can be registered after `Bridge.begin()`:

```cpp
#include <etl/array.h>
#include <pw_stream/memory_stream.h>
#include <pw_transfer/handler.h>

namespace {
constexpr uint32_t kResourceId = 0x1001;
etl::array<pw::byte, 32> resource_bytes{};
pw::stream::MemoryReader resource_reader{
    pw::ConstByteSpan(resource_bytes.data(), resource_bytes.size())};
pw::transfer::ReadOnlyHandler resource_handler{
    kResourceId, resource_reader};
}

void setup() {
  Bridge.begin();
  const bool registered =
      Bridge.transferService().RegisterHandler(resource_handler);
  if (!registered) {
    Bridge.enterSafeState();
  }
}

void loop() {
  Bridge.process();
}
```

The transfer service is registered with the upstream `pw_rpc::Server` by Bridge initialization. Applications register resource handlers through `transferService()`. For an outgoing read or write, use `transferClient()` and keep the stream reader/writer and completion callback alive until completion.

## Filesystem transfer boundary and Common Storage Architecture

As formalized in [ADR-0004](decisions/0004-linux-mpu-storage-ssot.md), the OpenWrt Linux MPU filesystem is the unified Single Source of Truth (SSOT) for persistent storage across the McuBridge ecosystem:

- **Cloud <-> Linux Filesystem:** Synchronized using upstream `pw_transfer` staging through `FileTransferService` (`PushToLinux` / `FetchFromLinux`). Legacy unary file streaming messages (`FileRead`, `FileWrite`) are eliminated from the public client contracts.
- **MCU Persisted Data:** The MCU operates in high-speed, deterministic SRAM (`DataStore`) and delegates large or persistent storage to the Linux MPU filesystem to prevent embedded NOR flash wear-out on modern targets (such as SAMD21 and ESP32).
- **AVR EEPROM Resource:** The ATmega2560 application registers a built-in upstream `pw_transfer` read/write handler for the internal EEPROM (resource ID `0x4D454741`). The resource covers the first 4,080 bytes; the last 16 bytes are reserved for the link boot epoch ([ADR-0005](decisions/0005-mcu-contributed-handshake-nonce.md)) and are not reachable through the transfer. It is exposed as a fixed-size raw byte resource, not as a filesystem: there are no directories, filenames, or per-file remove operations. It is a chip-specific hardware resource for AVR compatibility, not a cross-architecture storage contract.
  - The stream uses bounded random access and writes with `EEPROM.update`, avoiding physical writes when a byte is unchanged. To keep the UART receive path serviceable while the EEPROM controller performs a byte write, the handler advertises a one-byte write window; reads keep normal chunked transfer behavior.
  - EEPROM writes are not transactional: an interruption can leave a partially updated image, and each cell has finite endurance (nominally 100,000 write/erase cycles).
- **Cloud access to the EEPROM (ADR-0007):** a client stages or retrieves the image through the gateway resource `0x4D454741` (`eeprom.bin`, at most 4,080 bytes) and calls `rpc.pb.EepromService/Read` or `/Write`. The daemon copies the image between that staging slot and the MCU resource with its MCU `pw_transfer` client. Both operations require the `eeprom_read` / `eeprom_write` permissions and a gateway ACL rule. A write starts at offset 0, is not transactional, and with the one-byte window takes on the order of tens of seconds for a full image (Fleet Mesh logs the measured rate).
- **MCU → Linux uploads (ADR-0007, E6; AVR):** `FileSystem.upload(path, data)` exposes `data` read-only on the MCU resource `FILE_UPLOAD_RESOURCE_ID` (`0x55504C44`) and calls `FileSystemService.Upload{path, size}`. The daemon validates the path and `file_write_max_bytes`, reads the resource with its MCU `pw_transfer` client in a separate task (the serial read loop must keep feeding transfer packets), checks the size, stores the file under `file_system_root` with the quota, and completes the call with a status (`OK`, `PERMISSION_DENIED`, `OUT_OF_RANGE`, `DATA_LOSS`, `RESOURCE_EXHAUSTED`, `UNAVAILABLE`). The sketch buffer must stay valid until `FileSystem.uploadPending()` is false; one upload runs at a time. It costs about 120 bytes of static SRAM on the Mega.
- **MCU FileSystemService Unary Operations:** `FileSystemService.Write` remains a transitional legacy RPC on the MCU for bounded writes (path and data up to 64 bytes each, `mcu_wire.options`) from sketch code to the MPU filesystem; it does not access the EEPROM resource. `Remove` unlinks files within the safe root.

## Validation and release gates

- The port includes [avr_pigweed_port PR #8](https://github.com/isantolin/avr_pigweed_port/pull/8), [PR #10](https://github.com/isantolin/avr_pigweed_port/pull/10), [PR #11](https://github.com/isantolin/avr_pigweed_port/pull/11), and [PR #12](https://github.com/isantolin/avr_pigweed_port/pull/12), merged as `026c5f6b4350e75641bc125d2098e53c7e2d317f`. The Bridge PR submodule is pinned to that merge commit. Both CRC32 CI configurations passed in PR #10; the Bridge PR's final-head matrix remains a separate release gate.
- CI compiles both BridgeControl and a dedicated `pw_transfer` API sketch for Arduino Mega 2560 (default build, `pw_transfer` enabled).
- Fleet Mesh now runs the canonical sketch on simavr, initiates `FileSystem.upload()` from the Mega, and verifies the exact uploaded bytes under that node's isolated MPU `file_system_root`. This is the end-to-end MCU → serial/HDLC → daemon → MPU filesystem check; it does not replace a physical UART/HDLC test.
- The port CI exercises upstream transfer reads/writes, retries, cancellation, timeout, bounded-queue saturation, and sequential role reuse under `simavr`.
- The complete Bridge firmware's Flash, static SRAM, and worst-case stack must be measured from the Mega 2560 ELF. Standalone port measurements are not a substitute.
- Hardware UART/HDLC transfer against a live MPU remains a separate release gate.

This PR removes only the MCU `FileSystemService.Read` RPC. The `mcubridge-gateway` central Linux service still exposes the existing `LocalBridge.FileRead` / `FileWrite` APIs, and the edge MPU daemon still owns the actual device filesystem. Their `mcu/eeprom` route now uses upstream `pw_transfer`; ordinary paths still use the MPU filesystem.

The Linux endpoint is a **replacement Gateway**, not an extension or sidecar to the existing Python/grpclib Gateway. ADR-0003 defines the production target: one standalone C++ Linux service using upstream `pw::rpc::Server`, `pw::transfer::TransferService`, `pw_hdlc`, and mutually authenticated TLS. Python Pigweed packages remain clients.

**Current implementation status (2026-11-18):** the C++ Gateway foundation exists in `mcubridge-gateway-cpp`. It implements TLS 1.3 mutual authentication, SPIFFE URI-SAN identity extraction, separate client/device session registries, per-peer RPC/HDLC contexts, upstream `pw_transfer` registration, bounded staging resources, private client/device directories, an ACL-protected `GatewayControl.Dispatch`, and a health RPC. These components are not yet equivalent to the complete production Gateway contract.

**Still required before production cutover:** complete edge-initiated device session migration and bidirectional service coverage; prove the full set of cloud, telemetry, console, mailbox, process, GPIO, EEPROM, and file operations against the new Gateway; finish end-to-end transfer, cancellation, reconnect, and authorization tests; validate deployment and OpenWrt packaging; then remove the old `grpclib` server, `CloudBridge`/`LocalBridge` APIs, unary `FileRead`/`FileWrite`, and every consumer in one coordinated cutover. The former Python/grpclib Gateway and its dependency stack were removed in PR #85; they are not a supported fallback. The C++ service is the sole central Gateway. Its production readiness still depends on complete service-parity evidence, deployment validation, and the release gates above.