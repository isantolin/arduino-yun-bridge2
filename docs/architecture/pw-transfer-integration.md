# Upstream pw_transfer integration

The AVR build of McuBridge now exposes upstream Pigweed `pw_transfer` through the existing RPC/HDLC channel.

## Runtime ownership

- `BridgeClass` owns one `pw::transfer::Thread<1, 1>`.
- The same thread is shared by `Bridge.transferService()` and `Bridge.transferClient()`.
- `Bridge.process()` polls the thread with a bounded event budget after receiving serial/HDLC packets.
- Applications must not create another transfer thread or call `Poll()` themselves.
- The port's role arbiter permits one active transfer role at a time. Client and server transfers can be reused sequentially, not simultaneously.

The API is available on AVR builds:

```cpp
Bridge.transferService()
Bridge.transferClient()
```

## Registering an application resource

Handlers must have static or otherwise transfer-long lifetime. For example, a read-only in-memory resource can be registered after `Bridge.begin()`:

```cpp
#include <array>
#include <pw_stream/memory_stream.h>
#include <pw_transfer/handler.h>

namespace {
constexpr uint32_t kResourceId = 0x1001;
std::array<std::byte, 32> resource_bytes{};
pw::stream::MemoryReader resource_reader{
    pw::ConstByteSpan(resource_bytes)};
pw::transfer::ReadOnlyHandler resource_handler{
    kResourceId, resource_reader};
}

void setup() {
  Bridge.begin();
  const bool registered =
      Bridge.transferService().RegisterHandler(resource_handler);
  if (!registered) {
    // Handle duplicate/invalid resource registration.
  }
}

void loop() {
  Bridge.process();
}
```

The service is already registered with the upstream `pw_rpc::Server`; applications register individual resource handlers through `transferService()`.

For an outgoing read or write, use `Bridge.transferClient()` and keep the stream reader/writer and completion callback alive until completion. The main loop must continue calling `Bridge.process()` so the cooperative transfer state machine advances.

## Existing file RPC behavior

The existing `FileSystemService.Read` method remains Nanopb server-streaming. It already provides bounded chunked reads and is not replaced by `pw_transfer` merely for architectural uniformity. Use `pw_transfer` for resources that benefit from its transfer protocol, resumability/retry behavior, or application-defined handlers; avoid maintaining duplicate file-transfer paths without a concrete requirement.

## Memory and validation

The AVR port uses fixed-size buffers and a bounded cooperative event queue. The Bridge integration currently configures 128-byte thread buffers and a 256-byte transfer window. Validate the actual board's complete firmware SRAM/Flash and worst-case stack before enabling application handlers on a 32U4 with its 2.5 KiB SRAM budget.

The dependent port PR is [avr_pigweed_port PR #6](https://github.com/isantolin/avr_pigweed_port/pull/6). This Bridge change is intentionally a draft until the port's current-head CI and the Yún ATmega32U4 compile pass.
