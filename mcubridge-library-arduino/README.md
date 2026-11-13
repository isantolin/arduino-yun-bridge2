# McuBridge Arduino Library

[![C++ Standard](https://img.shields.io/badge/C%2B%2B-17-00599C?logo=cplusplus)](https://isocpp.org/)
[![ETL](https://img.shields.io/badge/ETL-20.39+-green)](https://www.etlcpp.com/)
[![SIL-2](https://img.shields.io/badge/IEC%2061508-SIL--2-brightgreen)](https://en.wikipedia.org/wiki/IEC_61508)
[![FIPS 140-3](https://img.shields.io/badge/MIL--SPEC-FIPS%20140--3-critical)](https://csrc.nist.gov/publications/detail/fips/140/3/final)
[![Arduino](https://img.shields.io/badge/Arduino-AVR%20|%20ESP32%20|%20SAMD-00979D?logo=arduino)](https://www.arduino.cc/)

This library provides the MCU-side runtime for the Arduino MCU Bridge v2 project. It complements the OpenWrt daemon by handling RPC frames, pin control, datastore access, mailbox messaging, filesystem helpers, and process control from the Arduino sketch.

## Directory Layout

- `src/`
  - Public headers available to sketches (`Bridge.h`, `Console.h`, `Mailbox.h`, etc.).
  - `services/`: implementation files and classes that depend on the Arduino core (e.g. `Bridge.cpp`).
  - `protocol/`: generated protocol constants (`rpc_protocol.h`, `rpc_hw_config.h`), Nanopb payload bindings (`mcu_wire.pb.h`/`.c`, `config_schema.pb.h`/`.c`), and upstream Pigweed Nanopb RPC service/client stubs (`mcu_wire.rpc.pb.h` generated via `tools/protocol/protoc_gen_mcu_rpc.py`).
- `examples/`
  - Arduino sketches demonstrating usage of the library:
    - `BridgeControl`: Standard UART/TTY wired bridge setup with GPIO, mailbox and process control.
    - `BridgeWiFi`: Wireless WiFi TCP stream bridge (`WiFiClient`) with automatic reconnection and keepalive.
    - `BridgeBluetooth`: Wireless Bluetooth SPP / BLE UART bridge (`BluetoothSerial`) with zero-wire setup.
    - `BridgeCloud`: Cloud-integrated bridge streaming live pin telemetry (`Bridge.sendPinEvent`) to `mcubridge-gateway`.
- `docs/`
  - Additional documentation and diagrams describing the protocol and library design (`../docs/PROTOCOL.md`).
- `tools/`
  - Maintenance scripts such as `install.sh` to deploy the library into an Arduino environment.
  - The protocol schemas live in `tools/protocol/` (`config_schema.proto`, `mcu_wire.proto`, `cloud_gateway.proto`); running `python3 tools/protocol/generate.py` refreshes the shared headers in `src/protocol/` alongside the Python daemon bindings.

## Installation

1. Run `tools/install.sh` to copy the library into your Arduino libraries folder.
2. Open Arduino IDE and locate the `BridgeControl`, `BridgeWiFi`, or `BridgeBluetooth` example under **File > Examples > McuBridge**.
3. Upload to your Arduino MCU to validate the end-to-end communication with the bridge daemon.

### External dependencies

- **PigweedAVR (`third_party/avr_pigweed_port`)**: Provides Google Pigweed modules adapted for bare-metal AVR/ARM/ESP32 (`pw_hdlc` framing with IEEE 802.3 CRC-32 FCS, upstream `pw_rpc` `pw::rpc::Server` and `pw::rpc::Client` and `pw::rpc::Channel` with `pw_rpc/nanopb` codegen, `pw_tokenizer` compile-time FNV-1a hashing, `pw_log` tokenized diagnostics via `pw::log::SetLogSink`, and `pw_chrono` atomic millisecond timekeeping via `pw::chrono::SystemClock`).
- **Cryptographic providers**: PSA supplies SHA-256/HMAC-SHA256 and Mbed TLS platform zeroization. HKDF-SHA256 and ChaCha20-Poly1305 use Mbed TLS 4 on AVR/SAMD and wolfCrypt 5.8.4 on ESP32.
- **Nanopb (0.4.9.2)**: High-efficiency, zero-heap protobuf serialization using `pb_decode_noinit` (`rpc::decode_pb`) and `pb_encode` (`rpc::encode_pb`) without redundant `memset` sweeps on statically allocated memory.
- **ETL (Embedded Template Library)**: Used for deterministic, static memory containers (`circular_buffer`, `vector`, `queue`, `span`) with strict Zero-STL (`-DETL_NO_STL`) compliance.

## SIL-2 & MIL-SPEC Design Principles

This library describes safety- and security-oriented design goals; it does not claim IEC 61508 SIL-2 certification or FIPS 140-3 validation:
- **No Dynamic Memory & Zero STL (`-DETL_NO_STL`):** All buffers are statically allocated using ETL and PigweedAVR static containers. No `malloc`/`new` or `<std::*>` headers in production firmware.
- **Modular `pw_rpc` Service Dispatch (`pw::rpc::Server` and `pw::rpc::Client` & `protoc_gen_mcu_rpc.py`):** The MCU registers 6 static Nanopb server services (`GpioRpcService`, `SystemRpcService`, `ClockSyncRpcService`, `SpiRpcService`, `FileSystemRpcService`, and `MailboxRpcService` deriving from `rpc::pb::pw_rpc::nanopb::<Service>::Service<Impl>`) over `pw::rpc::Channel` (`BridgeClass::RpcChannelOutput`) and `pw::rpc::Server` using compile-time `pw::tokenizer::Hash("rpc.pb.<Service>")` identifiers and `pw::rpc::NanopbServerWriter` for server streams. `ProcessService` and `DataStoreService` generate `rpc::pb::pw_rpc::nanopb::<Service>::Client` stubs for MCU-initiated requests to the Linux daemon; they are not registered as MCU server services.
- **Multi-Channel HDLC Framing (`pw_hdlc`):** Multiplexes `pw_rpc` packets (`HDLC_ADDRESS_RPC = 1`), tokenized logs (`HDLC_ADDRESS_LOG = 2`), and console streams (`HDLC_ADDRESS_CONSOLE = 3`) with IEEE 802.3 CRC-32 FCS integrity.
- **MCU data transfer:** The legacy Nanopb `FileSystemService.Read` stream has been removed. On ATmega2560, `Bridge.begin()` automatically registers the internal 4,096-byte EEPROM as a fixed-size raw resource (`pw_transfer` resource ID `0x4D454741`) using upstream `pw_transfer` through `Bridge.transferService()` / `Bridge.transferClient()`. The EEPROM is not a filesystem: it has no filenames or directories. Writes use `EEPROM.update` and a one-byte write window to limit blocking in the UART service loop; unchanged bytes do not consume EEPROM write cycles. The EEPROM has finite endurance (nominally 100,000 write/erase cycles per cell), and a power loss during transfer can leave a partially updated image. MCU unary `FileSystemService.Write` / `Remove` remain transitional legacy APIs and are not the EEPROM transfer interface.
- **Deterministic FSM:** Communication state managed by `etl::fsm` with explicit states (`STARTUP`, `UNSYNCHRONIZED`, `HANDSHAKE`, `SYNCHRONIZED`, `AWAITING_ACK`, `FAULT`).
- **Cryptographic Self-Tests (POST):** MCU Known-Answer Tests cover SHA-256, HMAC-SHA256, and ChaCha20-Poly1305. The daemon tests its AEAD library and encrypted transport behavior.
- **Encrypted link:** With a configured shared secret, RPC, console, and log HDLC payloads are encrypted after `LinkSync`. Empty-secret mode is explicitly unauthenticated and unencrypted. Per-session counters reject replay within a session; replay across MCU restarts is not ruled out.
- **Fail-Secure:** Initialization aborts and the system enters a safe state (`FAULT`) if cryptographic or SRAM March integrity checks fail.
- **Handshake and Session Key Separation:** Uses distinct HKDF info strings for host and MCU HMAC proofs, then derives independent directional ChaCha20-Poly1305 session keys using the handshake nonce and the `session-key` context.
- **Atomic Timekeeping (`pw::chrono::SystemClock`):** Uses `pw::chrono::SystemClock::now()` (backed by `pw_chrono` with 1 ms tick resolution) for deterministic timeout and interval tracking.
- **Zero-Copy Deserialization**: Uses `pb_decode_noinit` from Nanopb 0.4.9.2 (`rpc::decode_pb`) to eliminate initialization overhead on static memory structures.
- **Hardware Abstraction:** Automatic detection of MCU capabilities including GPIO limits, Big Buffer, EEPROM, DAC, FPU, I2C, SPI, WiFi, and Bluetooth.

## Best Practices

### Non-Blocking Loop
The Bridge library relies on frequent calls to `Bridge.process()` to handle incoming RPC frames, heartbeats, and handshake messages from the Linux daemon.
**Avoid using `delay()`** in your `loop()` or `setup()`. Blocking the MCU for more than a few milliseconds can cause the Serial buffer (64 bytes) to overflow, leading to packet corruption and connection instability.

Instead of:
```cpp
void loop() {
  // BAD: Blocks for 1 second, risking buffer overflow
  delay(1000);
}
```

Use non-blocking logic:
```cpp
void loop() {
  Bridge.process(); // Must be called frequently!

  static long lastRun = 0;
  if (millis() - lastRun > 1000) {
    lastRun = millis();
    // Do work...
  }
}
```

## Building From Source

- The library targets AVR, SAMD21, and ESP32 Arduino MCU boards alongside a host C++17 test/emulation target.
- The shared protocol headers are kept aligned with the Python daemon under `mcubridge/mcubridge/protocol`.
- All payload serialization uses **nanopb/protobuf** via generated `rpc_pb_*` structs in `mcu_wire.pb.h` / `config_schema.pb.h` encoded and decoded via `rpc::encode_pb` and `rpc::decode_pb`.
- MCU sketches should no longer attempt to initiate pin reads directly; GPIO reads are exclusively driven from the Linux daemon via Cloud/IPC requests (`GpioService/DigitalRead`/`GpioService/AnalogRead`), while asynchronous pin events are streamed via `Bridge.sendPinEvent` (`GpioService/SubscribePins`).

## Contributing

Patches and issues are welcome. Please run the example sketches and, if possible, unit tests before submitting changes.
