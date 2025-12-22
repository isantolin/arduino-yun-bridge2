# Arduino Yun Bridge Examples

The sketches under `openwrt-library-arduino/examples/` act as smoke tests for the MCU library after the November 2025 refresh.

## BridgeControl

- Exercises the main YunBridge flow: mailbox handling, GPIO callbacks, and `STATUS_*` frames from Linux.
- Uses `Bridge.onDigitalReadResponse`, `Bridge.onMailboxMessage`, and `Bridge.onStatus` to react to asynchronous events without busy loops.
- Handy to confirm that the Python daemon and the MCU share the same serial secret before layering more services.

## FrameDebug

- Build with `-DBRIDGE_DEBUG_FRAMES=1` (defined in `Bridge.h`) to emit TX counters.
- Sends `CMD_GET_FREE_MEMORY` every few seconds and prints frame stats (COBS/RAW lengths, CRC32, shortfalls) over Serial.
- Ideal for debugging MCU<->Linux synchronization issues and for tuning the daemon `serial_retry_*` knobs.

## Quick build and upload

Compile and upload any example via `arduino-cli`:

```sh
# Replace <SketchDir> with BridgeControl or FrameDebug
arduino-cli compile --fqbn arduino:avr:yun openwrt-library-arduino/examples/<SketchDir>
arduino-cli upload --fqbn arduino:avr:yun --port /dev/ttyACM0 \
  openwrt-library-arduino/examples/<SketchDir>
```

Tips:

1. Set `BRIDGE_SERIAL_SHARED_SECRET` in the sketch using the snippet from LuCI's *Credentials & TLS* tab (or `tools/rotate_credentials.sh`) before flashing.
2. PlatformIO users can point `src_dir` to the example inside an `arduino_yun` environment to reuse the same macros.
3. After uploading, open the 115200 baud serial monitor to watch the logs that correlate with the Python daemon.

## Suggested validation flow

1. Flash `BridgeControl.ino` and restart the daemon (`/etc/init.d/yunbridge restart`).
2. From Linux run `openwrt-yun-examples-python/mailbox_test.py` to send `ON`/`OFF` messages and verify the LED reacts.
3. Switch to `FrameDebug.ino` when you need to inspect timings or CRC32 values on the serial link; keep the serial console open for a few minutes to gather meaningful stats.

These steps keep the examples aligned with the modern stack (TLS enabled by default, strong handshake, and MQTT v5 topics) described in consulta [ARCHITECTURE.md](../../../docs/ARCHITECTURE.md)..
