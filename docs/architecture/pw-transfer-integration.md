# Upstream pw_transfer integration (Arduino Mega 2560)

The ATmega2560 build of McuBridge exposes upstream Pigweed `pw_transfer` through the existing `pw_rpc` / HDLC channel. `pw_transfer` is the only supported streaming-transfer protocol; the legacy `FileSystemService.Read` Nanopb stream has been removed from the schema, firmware, and host tests.

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

The EEPROM resource is registered automatically by `Bridge.begin()` on ATmega2560. Its stable resource ID is `0x4D454741` and its resource size is 4,096 bytes. Applications can still register additional resources, provided handlers and backing streams have static or otherwise transfer-long lifetime. For example, a read-only in-memory resource can be registered after `Bridge.begin()`:

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

## Filesystem transfer boundary

`FileSystemService.Read` no longer exists in the MCU service descriptor. Its Nanopb client reader, server writer, callbacks, observer cleanup, MCU read-stream error codes, and RPC tests were removed. `FileRead` / `FileReadResponse` remain only as message types for the MPU-local CloudBridge file API, along with its `file_read` authorization flag; that API does not forward `mcu/` paths over the removed serial RPC. The MCU `FileSystemService` bounded unary `Write` and `Remove` remain transitional APIs. `Write` is still used in the opposite direction by `FileSystem.write()` to upload data from the MCU into the MPU Linux filesystem; it does not access the EEPROM resource. Remove that remaining unary path only after a Linux-side `pw_transfer` server and the MCU transfer-client path are implemented and tested. `Remove` remains a separate filesystem administration operation.

Large-file reads and writes must use upstream `pw_transfer` with a resource handler backed by a bounded stream. The ATmega2560 application registers a built-in upstream `pw_transfer` read/write handler for the internal 4,096-byte EEPROM. It exposes the EEPROM as one fixed-size raw byte resource (resource ID `0x4D454741`), not as a filesystem: there are no directories, filenames, or per-file remove operations. The stream uses bounded random access and writes with `EEPROM.update`, avoiding physical writes when a byte is unchanged. To keep the UART receive path serviceable while the AVR EEPROM controller performs a byte write, the handler advertises a one-byte write window; reads retain normal chunked transfer behavior. EEPROM writes are not transactional: interruption can leave a partially updated image, and the internal EEPROM has finite per-cell endurance (nominally 100,000 write/erase cycles). This backend does not make the separate MPU Linux filesystem APIs operational; those still require the Linux-side `pw_transfer` endpoint and client migration.

## Validation and release gates

- The port includes [avr_pigweed_port PR #8](https://github.com/isantolin/avr_pigweed_port/pull/8) and the exporter compatibility fixes from [PR #10](https://github.com/isantolin/avr_pigweed_port/pull/10), merged as `511814763baf9b748f4663f05e5bf6d4cab2d31b`. The Bridge PR submodule is pinned to that merge commit. Both CRC32 CI configurations passed in PR #10; the Bridge PR's final-head matrix remains a separate release gate.
- CI compiles both BridgeControl and a dedicated `pw_transfer` API sketch for Arduino Mega 2560.
- The port CI exercises upstream transfer reads/writes, retries, cancellation, timeout, bounded-queue saturation, and sequential role reuse under `simavr`.
- The complete Bridge firmware's Flash, static SRAM, and worst-case stack must be measured from the Mega 2560 ELF. Standalone port measurements are not a substitute.
- Hardware UART/HDLC transfer against a live MPU remains a separate release gate.

This PR removes only the MCU `FileSystemService.Read` RPC. The `mcubridge-gateway` central Linux service still exposes the existing `LocalBridge.FileRead` / `FileWrite` APIs, and the edge MPU daemon still owns the actual device filesystem. Their `mcu/eeprom` route now uses upstream `pw_transfer`; ordinary paths still use the MPU filesystem.

The Linux endpoint is a **replacement Gateway**, not an extension of the existing Python/grpclib Gateway. ADR-0003 defines the target: one standalone C++ Linux service using upstream `pw::rpc::Server`, `pw::transfer::TransferService`, `pw_hdlc`, and mutually authenticated TLS. Python Pigweed packages remain clients. The new Gateway owns authenticated per-connection RPC contexts and transfer resources; the MPU edge remains the authority for its local filesystem and exposes bounded operations through its Pigweed session. The old `grpclib` server, `CloudBridge`/`LocalBridge` APIs, unary `FileRead`/`FileWrite`, and their consumers are removed in the same production cutover after feature parity and security tests pass. No sidecar, compatibility endpoint, parallel Gateway, or fallback is permitted. The native Gateway, edge session migration, service implementations, bounded file handlers, deployment, and end-to-end validation are not implemented yet.