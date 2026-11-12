# Upstream pw_transfer integration (Arduino Mega 2560)

The ATmega2560 build of McuBridge exposes upstream Pigweed `pw_transfer` through the existing `pw_rpc` / HDLC channel. This is an optional application-resource API; it does not replace the existing file RPC.

## Runtime ownership

- `BridgeClass` owns one `pw::transfer::Thread<1, 1>`.
- The AVR thread uses a 128-byte chunk buffer and a 256-byte encode buffer, satisfying upstream's requirement that the encode buffer exceed the chunk buffer and cover the configured frame size.
- The Python protocol generator registers the upstream `pw.transfer.Transfer` descriptor from Pigweed's `pw_transfer.transfer_pb2`, so the daemon RPC client can resolve `Read`, `Write`, and `GetResourceStatus`.
- A Python consumer can create `pw_transfer.Manager(serial_transport.rpc_client.channel(1).rpcs.pw.transfer.Transfer, max_chunk_size_bytes=128)`; the 128-byte chunk cap matches the AVR staging buffer.
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

Handlers and their backing streams must have static or otherwise transfer-long lifetime. For example, a read-only in-memory resource can be registered after `Bridge.begin()`:

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

## Existing file RPC behavior

`FileSystemService.Read` remains Nanopb server-streaming. It already provides bounded chunked file reads and is not replaced by `pw_transfer`. Use `pw_transfer` for separate application resources that benefit from its transfer protocol, retries, or resumability. Do not maintain duplicate file-transfer paths without a concrete requirement.

## Validation and release gates

- The port includes [avr_pigweed_port PR #8](https://github.com/isantolin/avr_pigweed_port/pull/8), merged as `1b1485fd256f28fc1fd71350598b6ec0f259701f`, plus the host-assert/export corrections from [PR #9](https://github.com/isantolin/avr_pigweed_port/pull/9), currently pinned at candidate commit `4fc9a831005c57bea911f20194261b13a4949a7c`.
- CI compiles both BridgeControl and a dedicated `pw_transfer` API sketch for Arduino Mega 2560.
- The port CI exercises upstream transfer reads/writes, retries, cancellation, timeout, bounded-queue saturation, and sequential role reuse under `simavr`.
- The complete Bridge firmware's Flash, static SRAM, and worst-case stack must be measured from the Mega 2560 ELF. Standalone port measurements are not a substitute.
- Hardware UART/HDLC transfer against a live MPU remains a separate release gate.

The existing `FileSystemService.Read` and MPU contracts remain unchanged.