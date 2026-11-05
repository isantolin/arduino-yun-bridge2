# MCU Bridge 2.8.9 — Release Notes (draft)

**Status:** Draft for PR #58. Do not publish until the PR is merged and the complete CI, emulation, and packaging checks pass.

## Highlights

- **Google Pigweed Serial Transport & RPC Migration (`pw_hdlc` + `pw_rpc`)**:
  - Replace legacy single-channel COBS framing and custom `RpcEnvelope` headers with 3-channel `pw_hdlc` UI frames (`HDLC_ADDRESS_RPC = 1`, `HDLC_ADDRESS_LOG = 2`, `HDLC_ADDRESS_CONSOLE = 3`) protected by IEEE 802.3 CRC-32 FCS.
  - Implement 8 modular `pw::rpc_avr::RpcService` subclasses on the MCU registered with `pw::rpc_avr::RpcServer` using compile-time `pw::tokenizer::Hash("rpc.pb.<Service>")` FNV-1a identifiers and server-streaming pin events (`GpioService/SubscribePins`).
  - Use `PW_SERVICES` (`pw_rpc.descriptors.Services`) and `pw_rpc.packets` (`decode`, `for_server`, `decode_payload`) directly in Python without custom protocol registry wrappers.
  - Integrate `pw_chrono_avr` (`pw_chrono_avr_GetMillis()`) for atomic millisecond timekeeping and `pw_log_avr` / `pw_tokenizer` decoded via `pw_tokenizer.Detokenizer`.
- Reduce event-loop blocking by moving LMDB transactions, compaction, lifecycle operations, and filesystem work to worker threads.
- Cache persistent deque length so queue-depth reads are O(1), while serializing deque mutations and lifecycle operations.
- Stream local file transfers to the MCU in bounded chunks and preserve the protocol's empty end-of-file frame.
- Accumulate MCU file-read chunks in a single `bytearray` and materialize the unary gRPC response once.
- Use `asyncio.timeout` and `asyncio.TaskGroup` for runtime deadlines and serial task supervision.
- Correct transport capability reporting: the current grpclib implementation uses HTTP/2; an HTTP/3 request warns and falls back to HTTP/2 instead of reporting a false QUIC connection.

## Compatibility and migration

- **MCU ↔ MPU Serial Wire Protocol**: Both the Linux daemon (`mcubridge`) and the Arduino MCU sketch (`mcubridge-library-arduino`) must be upgraded together to `v2.8.9` (`pw_hdlc` + `pw_rpc`).
- **Northbound & Local IPC Contracts**: `GatewayLocalBridgeService`, `CloudBridge` gRPC, OpenWrt `ubus` (`mcubridge`), and `luci-app-mcubridge` remain unchanged.
- HTTP/3 is not implemented; deployments requesting it continue over HTTP/2 with an explicit warning.

## Validation status

- Verified across Python unit/property/integration tests, C++ host unit tests (`>= 95.0%` coverage), and hardware-in-the-loop emulation (`simavr` ATmega2560/328P/32U4, `Renode` SAMD21, `QEMU` ESP32-S3, and Docker 3-node Mesh).
- Validate file-transfer behavior and runtime stability on a physical Arduino Yún / YúnShield before declaring the release production-ready.

## Release procedure

1. Merge PR #58 only after all required CI checks pass and explicit maintainer authorization is given.
2. Confirm package metadata and runtime `__version__` both report `2.8.9`.
3. Create tag `v2.8.9` on the merged commit.
4. The `Publish Python Package to GitHub Packages` workflow builds the source distribution and wheel and attaches them to the GitHub Release.
5. Verify release assets and checksums before announcing the release.
