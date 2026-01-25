# McuBridge Arduino Library

This library provides the MCU-side runtime for the Arduino MCU Bridge v2 project. It complements the OpenWrt daemon by handling RPC frames, pin control, datastore access, mailbox messaging, filesystem helpers, and process control from the Arduino sketch.

## Directory Layout

- `src/`
  - Public headers available to sketches (`Bridge.h`, `Console.h`, `Mailbox.h`, etc.).
  - `arduino/`: implementation files and classes that depend on the Arduino core (e.g. `Bridge.cpp`).
  - `protocol/`: protocol helpers shared with the Linux daemon (COBS encoder, CRC, frame builder).
- `examples/`
  - Arduino sketches demonstrating usage of the library (`BridgeControl`).
- `docs/`
  - Additional documentation and diagrams describing the protocol and library design (`docs/PROTOCOL.md`).
- `tools/`
  - Maintenance scripts such as `install.sh` to deploy the library into an Arduino environment.
  - The protocol generator lives in `tools/protocol/spec.toml` (see repository root); running `python3 tools/protocol/generate.py` refreshes the shared headers in `src/protocol/` alongside the Python daemon bindings.

## Installation

1. Run `tools/install.sh` to copy the library into your Arduino libraries folder.
2. Open Arduino IDE and locate the `BridgeControl` example under **File > Examples > McuBridge**.
3. Upload to your Arduino MCU to validate the end-to-end communication with the bridge daemon.

### External dependencies

- **Crypto**: The installer fetches the [Crypto](https://github.com/rweather/arduinolibs/tree/master/libraries/Crypto) library from `rweather/arduinolibs`. That package exposes the same `<HMAC.h>` / `<SHA256.h>` interfaces.
- **Internalized Dependencies**: The library now includes internal implementations for **COBS framing** and **CRC32** (IEEE 802.3) to ensure strict binary compatibility with the Python daemon and remove external dependency risks.

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

- The library targets AVR-based Arduino MCU boards. Ensure the Arduino AVR core is installed.
- The shared protocol headers are kept aligned with the Python daemon under `openwrt-mcu-bridge/mcubridge/rpc`.
- Recent updates align the datastore, mailbox, and filesystem payloads with the binary protocol specification (length-prefixed values and `STATUS_*` propagation). The async process helpers now queue partial outputs so repeated `Bridge.processPoll()` calls deliver the full stream, and the library automatically issues additional polls when partial chunks arrive.
- MCU sketches should no longer attempt to initiate pin reads directly; GPIO reads are exclusively driven from the Linux daemon via MQTT (`CMD_DIGITAL_READ`/`CMD_ANALOG_READ`).

## Contributing

Patches and issues are welcome. Please run the example sketches and, if possible, unit tests before submitting changes.
