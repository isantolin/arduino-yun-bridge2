# Arduino MCU Bridge Examples

The sketches under `mcubridge-library-arduino/examples/` act as smoke tests for the MCU library after the November 2025 refresh.

## BridgeControl

- Exercises the main McuBridge flow: mailbox handling, GPIO callbacks, and `STATUS_*` frames from Linux.
- Uses `Bridge.onDigitalReadResponse`, `Bridge.onMailboxMessage`, and `Bridge.onStatus` to react to asynchronous events without busy loops.
- Handy to confirm that the Python daemon and the MCU share the same serial secret before layering more services.

## Quick build and upload

Compile and upload any example via `arduino-cli`:

```sh
# Replace <SketchDir> with BridgeControl
arduino-cli compile --fqbn arduino:avr:mcu mcubridge-library-arduino/examples/<SketchDir>
arduino-cli upload --fqbn arduino:avr:mcu --port /dev/ttyACM0 \
  mcubridge-library-arduino/examples/<SketchDir>
```

Tips:

1. Set `BRIDGE_SERIAL_SHARED_SECRET` in the sketch using the snippet from LuCI's *Credentials & TLS* tab (or `tools/rotate_credentials.sh`) before flashing.
2. PlatformIO users can point `src_dir` to the example inside an `arduino_yun` environment to reuse the same macros.
3. After uploading, open the 115200 baud serial monitor to watch the logs that correlate with the Python daemon.

## Suggested validation flow

1. Flash `BridgeControl.ino` and restart the daemon (`/etc/init.d/mcubridge restart`).
2. From Linux run `mcubridge-client-examples/mailbox_read_test.py` to send `ON`/`OFF` messages and verify the LED reacts.
3. When you need frame-level diagnostics, use `tools/frame_debug.py` from Linux to inspect COBS/CRC behavior without relying on sketch-only debug APIs.

These steps keep the examples aligned with the modern stack (TLS enabled by default, strong handshake, and MQTT v5 topics) described in [PROTOCOL.md](../../../docs/PROTOCOL.md).
