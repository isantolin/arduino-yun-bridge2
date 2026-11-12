# ADR-0002: Migration of MCU-MPU Serial Transport and RPC to Google Pigweed

- Status: Accepted
- Date: 2026-11-02
- Decision owners: Repository maintainer

## Context

The legacy MCU-MPU serial link in `arduino-yun-bridge2` relied on custom single-channel COBS (`0x00`-delimited) framing (`PacketSerial` / `rpc_frame.h`), a bespoke `RpcEnvelope` header (`command_id`, `sequence_id`, `payload`, CRC-32), a static code-generated `switch (command_id)` dispatch table (`rpc_dispatch_table.inc`), and manual correlation for unsolicited pin events and console bytes. That design required maintaining custom framing/checksum parsers in both C++ and Python, coupled every wire message to a single channel, and duplicated RPC dispatch infrastructure already standardized by Google Pigweed.

## Decision

1. **Adopt `pw_hdlc` as the canonical serial link layer** between the MCU (`mcubridge-library-arduino`) and the OpenWrt MPU daemon (`mcubridge`) with three logical HDLC UI frame channels:
   - Channel `1` (`HDLC_ADDRESS_RPC = 1`): `pw_rpc` (`RpcPacket` requests, responses, server streams, and errors).
   - Channel `2` (`HDLC_ADDRESS_LOG = 2`): `pw_log` / `pw_tokenizer` tokenized MCU diagnostics decoded via `pw_tokenizer.Detokenizer`.
   - Channel `3` (`HDLC_ADDRESS_CONSOLE = 3`): transparent bidirectional `Console` byte stream.
2. **Adopt upstream `pw_rpc` (`pw::rpc::Server` and `pw::rpc::Client`, `pw::rpc::Channel`, and `protoc_gen_mcu_rpc.py` Nanopb stubs on MCU; `pw_rpc.descriptors.Services` + `pw_rpc.internal.packet_pb2.RpcPacket` on MPU)**:
   - Define canonical Protobuf services in `tools/protocol/mcu_wire.proto` under package `rpc.pb` (`GpioService`, `SystemService`, `ClockSyncService`, `SpiService`, `FileSystemService`, `MailboxService`, `ProcessService`, `DataStoreService`).
   - Generate upstream Pigweed Nanopb service and client bindings (`mcubridge-library-arduino/src/protocol/mcu_wire.rpc.pb.h`) via `tools/protocol/protoc_gen_mcu_rpc.py` (`pw_rpc.codegen_nanopb`) and internal packet headers (`mcubridge-library-arduino/src/pw_rpc/internal/packet.pwpb.h`) via Pigweed's PWPB compiler.
   - Register six MCU-hosted server services in `mcubridge-library-arduino/src/Bridge.cpp` deriving from `rpc::pb::pw_rpc::nanopb::<Service>::Service<Impl>` (`GpioService`, `SystemService`, `ClockSyncService`, `SpiService`, `FileSystemService`, and `MailboxService`) over `pw::rpc::Channel` (`BridgeClass::RpcChannelOutput : public pw::rpc::ChannelOutput`) and `pw::rpc::Server` and `pw::rpc::Client`. `ProcessService` and `DataStoreService` provide generated `rpc::pb::pw_rpc::nanopb::<Service>::Client` contracts for requests initiated by the MCU toward the Linux daemon; they are not registered MCU server services. Dispatch uses compile-time `pw::tokenizer::Hash("rpc.pb.<Service>")` and `pw::tokenizer::Hash("<Method>")` FNV-1a identifiers.
   - Use `pw::rpc::NanopbServerWriter<rpc_pb_PinUpdateEvent>` / `SERVER_STREAM` packets on `rpc.pb.GpioService/SubscribePins` for asynchronous GPIO pin update notifications (`PinUpdateEvent`).
   - Construct `PW_SERVICES` (`pw_rpc.descriptors.Services`) directly from `(mcu_wire_pb2, cloud_gateway_pb2)` descriptors in `mcubridge/mcubridge/protocol/protocol.py` so `OperationSpec` and `build_local_bridge_mapping` resolve `service_id`, `method_id`, `request_type`, `response_type`, `cardinality`, and `default_factory` natively from Pigweed descriptors at runtime without custom wrapper registries.
3. **Integrate `avr_pigweed_port` (`third_party/avr_pigweed_port`) as a Git submodule** exported to the `PigweedAVR` Arduino library, maintaining strict Zero-STL (`-DETL_NO_STL`) and zero dynamic allocation compliance across 8-bit AVR (`avr-gcc`), 32-bit ARM Cortex-M0+ (`arm-none-eabi-gcc` GCC 7.2.1), 32-bit Xtensa ESP32-S3, and Host GCC tests.
4. **Integrate `pw::chrono::SystemClock` and `pw::log` C++ facades in `BridgeClass`**:
   - Use `pw::chrono::SystemClock::now()` (backed by `pw_chrono` with 1 ms tick period) for atomic millisecond timekeeping across watchdog, frame reception, timer ticks, handshake synchronization, and pin subscription tasks.
   - Route tokenized logs via the C++ sink facade `pw::log::SetLogSink` (`pw::ConstByteSpan`) to `BridgeClass::sendLogStream` on HDLC Channel `2`.
5. **Preserve upper-layer MPU contracts**:
   - Keep `LocalBridge` gRPC over Unix domain socket, OpenWrt `ubus` (`mcubridge` UBUS object), `luci-app-mcubridge`, and `CloudBridge` mTLS gRPC interfaces unchanged.

## Alternatives considered

- **Retain custom COBS + `RpcEnvelope` + static `switch` dispatch**: rejected because it lacks native logical channel multiplexing for console/logs, requires custom framing/CRC maintenance on both sides, and lacks native server-streaming semantics.
- **Full upstream Pigweed GN/Bazel build for AVR**: rejected because upstream Pigweed assumes full C++17 STL headers unavailable on `avr-gcc` without `avr_pigweed_port`'s freestanding polyfills and Arduino library packaging.
- **Precalculate and hardcode FNV-1a service/method hashes in `generate.py` with custom `"bridge.*"` prefixes**: rejected in favor of canonical `rpc.pb.<Service>` descriptors backed at runtime by `pw_rpc.descriptors.Services`.

## Consequences

- Legacy `rpc_frame.h`, `rpc_structs.h`, and `rpc_dispatch_table.inc` are removed from the MCU firmware.
- Console traffic (`HDLC_ADDRESS_CONSOLE = 3`) and tokenized logs (`HDLC_ADDRESS_LOG = 2`) are isolated from `pw_rpc` control traffic (`HDLC_ADDRESS_RPC = 1`) at the HDLC framing layer.
- Both Python and C++ test suites maintain `>= 95.0%` line, function, and branch coverage with hardware-in-the-loop emulation verified across `simavr` (ATmega2560 application target), `Renode` (SAMD21), and `QEMU` (ESP32-S3).

## Migration and rollback

- The wire protocol between MCU and MPU requires both endpoints to run the Pigweed HDLC + `pw_rpc` firmware and daemon (`v2.8.9+`).
- Upper-layer clients (`mcubridge-client-examples`, `luci-app-mcubridge`, `mcubridge-gateway`) require no migration.
- Rollback requires reverting both the `mcubridge` daemon and `mcubridge-library-arduino` sketch firmware together to the pre-Pigweed release.
