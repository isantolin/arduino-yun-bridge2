# McuBridge Arduino Library

This library provides the MCU-side runtime for the Arduino MCU Bridge v2 project. It complements the OpenWrt daemon by handling RPC frames, pin control, datastore access, mailbox messaging, filesystem helpers, and process control from the Arduino sketch.

**Modernization Update (Jan 2026):** The library now enforces **Static Memory Allocation** (via ETL) and **Cooperative Multitasking** (via TaskScheduler) for SIL-2 compliance and deterministic behavior.

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

The library depends on:
*   **Embedded Template Library (ETL):** For safe, static containers (`etl::vector`, `etl::circular_buffer`). No dynamic allocation.
*   **TaskScheduler:** For cooperative multitasking inside `Bridge.process()`.
*   **FastCRC:** High-performance CRC calculation.
*   **PacketSerial:** Framing support.
*   **Crypto:** For handshake HMAC-SHA256 authentication.

The installer script (`tools/install.sh`) attempts to fetch or verify these dependencies.

## Best Practices

### Cooperative Multitasking
The Bridge library uses `TaskScheduler` internally to manage serial processing and watchdog resets. You must call `Bridge.process()` in your `loop()` as frequently as possible.

**Do NOT use `delay()`**. Blocking the loop prevents the internal scheduler from running, causing serial buffer overflows and watchdog timeouts.

Instead of:
```cpp
void loop() {
  // BAD: Blocks EVERYTHING for 1 second
  delay(1000);
}
```

Use non-blocking logic (or `TaskScheduler` yourself):
```cpp
void loop() {
  Bridge.process(); // Runs the internal scheduler (Serial, Watchdog)

  static unsigned long lastRun = 0;
  if (millis() - lastRun > 1000) {
    lastRun = millis();
    // Do work...
  }
}
```

### Static Memory
This library does **not** use `malloc` or `new` after initialization. All buffers are statically allocated using ETL. This prevents heap fragmentation and ensures predictable memory usage, critical for long-running embedded systems (SIL-2).

## Building From Source

- The library targets AVR-based Arduino MCU boards (and supports others via standard Arduino API).
- The shared protocol headers are kept aligned with the Python daemon under `openwrt-mcu-bridge/mcubridge/rpc`.
- Recent updates align the datastore, mailbox, and filesystem payloads with the binary protocol specification (length-prefixed values and `STATUS_*` propagation). The async process helpers now queue partial outputs so repeated `Bridge.processPoll()` calls deliver the full stream, and the library automatically issues additional polls when partial chunks arrive.
- MCU sketches should no longer attempt to initiate pin reads directly; GPIO reads are exclusively driven from the Linux daemon via MQTT (`CMD_DIGITAL_READ`/`CMD_ANALOG_READ`).

## Contributing

Patches and issues are welcome. Please run the example sketches and, if possible, unit tests before submitting changes.