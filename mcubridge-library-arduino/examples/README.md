# Arduino MCU Bridge Examples

The sketches under `mcubridge-library-arduino/examples/` act as smoke tests for the MCU library after the November 2025 refresh.

## BridgeControl

- Exercises the main McuBridge flow over UART/TTY: mailbox handling, GPIO callbacks, and `STATUS_*` frames from Linux.
- Uses `Bridge.onDigitalReadResponse`, `Bridge.onMailboxMessage`, and `Bridge.onStatus` to react to asynchronous events without busy loops.
- Handy to confirm that the Python daemon and the MCU share the same serial secret before layering more services.

## BridgeWiFi

- Implements a wireless TCP bridge over WiFi (ESP32, SAMD, WiFiNINA, WiFi101).
- Connects to an access point, establishes a TCP stream to the OpenWrt daemon (e.g. `wifi://192.168.1.1:9000`), and forwards frames with automatic reconnection.
- Demonstrates how to pass a `WiFiClient` directly to `Bridge.begin(client)`.

## BridgeBluetooth

- Implements a wireless Bluetooth SPP (Serial Port Profile) or BLE UART bridge.
- Provides a wireless bridge interface for cable-free deployment (e.g. ESP32 `BluetoothSerial`).
- Uses the Nano ESP32's `Serial1` for an external transparent Bluetooth UART module; its ESP32-S3 does not provide Classic SPP through `BluetoothSerial`.
- Connects transparently to the daemon running on `/dev/rfcomm0` or a virtual serial port.

## BridgeCloud

- Reference implementation for connecting to the centralized `mcubridge-gateway` cloud hub.
- Demonstrates real-time sensor/pin telemetry streaming via `Bridge.sendPinEvent()` over the `CHANNEL_TELEMETRY` channel.
- Handles remote actuator and control RPCs issued by northbound cloud clients and correlated via `sequence_id`.
- If the local telemetry send fails, the example turns off `LED_BUILTIN` and, when Console support is enabled, emits a diagnostic through the Bridge Console. A successful local send is not a cloud-delivery acknowledgement.

## Quick build and upload

Compile and upload any example via `arduino-cli`:

```sh
# Replace <SketchDir> with BridgeControl
arduino-cli compile --fqbn arduino:avr:mcu mcubridge-library-arduino/examples/<SketchDir>
arduino-cli upload --fqbn arduino:avr:mcu --port /dev/ttyACM0 \
  mcubridge-library-arduino/examples/<SketchDir>
```

The CI compilation matrix builds every example for Arduino Mega 2560, MKR WiFi 1010, and Nano ESP32. Cycle-accurate simavr emulation remains AVR-only and runs on the Mega; it does not emulate the SAMD or ESP32 boards.

Tips:

1. Set `BRIDGE_SERIAL_SHARED_SECRET` in the sketch using the snippet from LuCI's *Credentials & TLS* tab (or `python3 tools/hardware_harness.py rotate`) before flashing.
2. PlatformIO users can point `src_dir` to the example inside an `arduino_yun` environment to reuse the same macros.
3. The Bridge UART carries COBS-framed binary traffic; do not attach a text serial monitor to that port while the Bridge is running. Use the Bridge Console/Gateway for text diagnostics, or `tools/frame_debug.py` for frame-level inspection from Linux.

## Suggested validation flow

1. Flash `BridgeControl.ino` and restart the daemon (`/etc/init.d/mcubridge restart`).
2. From Linux run `mcubridge-client-examples/mailbox_read_test.py` to send `ON`/`OFF` messages and verify the LED reacts.
3. When you need frame-level diagnostics, use `tools/frame_debug.py` from Linux to inspect COBS/CRC behavior without relying on sketch-only debug APIs.

These steps keep the examples aligned with the modern stack (TLS enabled by default, strong handshake, and Cloud topics) described in [PROTOCOL.md](../../../docs/PROTOCOL.md).
