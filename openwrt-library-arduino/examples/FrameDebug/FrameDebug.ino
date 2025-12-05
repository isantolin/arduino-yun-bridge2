// Match BridgeControl's initialization pattern: disable optional subsystems
// (they are unused here) and define the shared secret locally.
#define BRIDGE_ENABLE_DATASTORE 0
#define BRIDGE_ENABLE_FILESYSTEM 0
#define BRIDGE_ENABLE_PROCESS 0
#define BRIDGE_SECRET "changeme123"

#include <Bridge.h>

// Demonstrates how to inspect frame transmission statistics collected by
// BridgeClass when BRIDGE_DEBUG_FRAMES is enabled.

namespace {
const unsigned long kSendIntervalMs = 5000;
unsigned long last_send_ms = 0;
}

void setup() {
  Serial.begin(115200);
  while (!Serial) {
    // Wait for the USB serial console to be ready.
  }

  Serial.println(F("[FrameDebug] Starting"));

  Bridge.begin(115200, BRIDGE_SECRET);
  Serial.println(F("[FrameDebug] Bridge initialized with sketch-defined secret"));
}

void loop() {
  Bridge.process();

#if BRIDGE_DEBUG_FRAMES
  unsigned long now = millis();
  if (now - last_send_ms >= kSendIntervalMs) {
    last_send_ms = now;

    Serial.println(F("[FrameDebug] Sending CMD_GET_FREE_MEMORY"));
    Bridge.requestGetFreeMemory();

    delay(20);  // Allow time for the frame to flush over Serial1.

    BridgeClass::FrameDebugSnapshot snapshot = Bridge.getTxDebugSnapshot();
    Serial.println(F("[FrameDebug] --- TX Snapshot ---"));
    Serial.print(F("cmd_id=0x"));
    Serial.println(snapshot.command_id, HEX);
    Serial.print(F("payload_len="));
    Serial.println(snapshot.payload_length);
    Serial.print(F("crc=0x"));
    Serial.println(snapshot.crc, HEX);
    Serial.print(F("raw_len="));
    Serial.println(snapshot.raw_length);
    Serial.print(F("cobs_len="));
    Serial.println(snapshot.cobs_length);
    Serial.print(F("expected_serial_bytes="));
    Serial.println(snapshot.expected_serial_bytes);
    Serial.print(F("last_write_return="));
    Serial.println(snapshot.last_write_return);
    Serial.print(F("last_shortfall="));
    Serial.println(snapshot.last_shortfall);
    Serial.print(F("tx_count="));
    Serial.println(snapshot.tx_count);
    Serial.print(F("write_shortfall_events="));
    Serial.println(snapshot.write_shortfall_events);
    Serial.print(F("build_failures="));
    Serial.println(snapshot.build_failures);

    Bridge.resetTxDebugStats();
    Serial.println(F("[FrameDebug] Snapshot cleared"));
  }
#else
  // If BRIDGE_DEBUG_FRAMES is disabled, let users know the snapshot API
  // is not available.
  static bool notified = false;
  if (!notified) {
    Serial.println(F("[FrameDebug] BRIDGE_DEBUG_FRAMES disabled; enable it to collect stats."));
    notified = true;
  }
#endif
}
