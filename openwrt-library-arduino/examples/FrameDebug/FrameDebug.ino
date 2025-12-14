// Match BridgeControl's initialization pattern: disable optional subsystems
// (they are unused here) and define the shared secret locally.
#define BRIDGE_ENABLE_DATASTORE 0
#define BRIDGE_ENABLE_FILESYSTEM 0
#define BRIDGE_ENABLE_PROCESS 0
#define BRIDGE_SECRET "ba8dde66d745f63eb9514d32bba976a92d90136edceea701288e944e85830d94"

// When set to 1 the sketch automatically sends CommandId::CMD_GET_FREE_MEMORY frames
// every kSendIntervalMs milliseconds (behaviour prior to this change).
// Leaving it at 0 keeps the link quiet unless you trigger a command manually
// from the USB serial console (see the loop() implementation below).
#ifndef FRAME_DEBUG_AUTO_POLL
#define FRAME_DEBUG_AUTO_POLL 0
#endif

#include <Bridge.h>

// Demonstrates how to inspect frame transmission statistics collected by
// BridgeClass when BRIDGE_DEBUG_FRAMES is enabled.

namespace {
#if BRIDGE_DEBUG_FRAMES
const unsigned long kSendIntervalMs = 5000;
unsigned long last_send_ms = 0;

void printSnapshot(const BridgeClass::FrameDebugSnapshot &snapshot) {
  Serial.println(F("[FrameDebug] --- TX Snapshot ---"));
  Serial.print(F("cmd_id=0x"));
  Serial.println(snapshot.last_command_id, HEX);
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
}

void clearSnapshotStats() {
  Bridge.resetTxDebugStats();
  Serial.println(F("[FrameDebug] Snapshot cleared"));
}
#endif
}

void setup() {
  Serial.begin(115200);
  // NOTE: Removed blocking wait for Serial to allow daemon handshake
  // independent of USB connection.
  /*
  while (!Serial) {
    // Wait for the USB serial console to be ready.
  }
  */

  Serial.println(F("[FrameDebug] Starting"));

  Bridge.begin(115200, BRIDGE_SECRET);
  Serial.println(F("[FrameDebug] Bridge initialized with sketch-defined secret"));

  // Wait for handshake with non-blocking LED blink
  pinMode(13, OUTPUT);
  long lastBlink = 0;
  bool ledState = false;
  while (!Bridge.isSynchronized()) {
    Bridge.process();
    if (millis() - lastBlink > 50) {
      lastBlink = millis();
      ledState = !ledState;
      digitalWrite(13, ledState ? HIGH : LOW);
    }
  }
  Serial.println(F("[FrameDebug] Handshake synchronized"));
}

void loop() {
  Bridge.process();

#if BRIDGE_DEBUG_FRAMES
#if FRAME_DEBUG_AUTO_POLL
  unsigned long now = millis();
  if (now - last_send_ms >= kSendIntervalMs) {
    last_send_ms = now;

    Serial.println(F("[FrameDebug] Sending CommandId::CMD_GET_FREE_MEMORY"));
    Bridge.requestGetFreeMemory();

    delay(20);  // Allow time for the frame to flush over Serial1.

    BridgeClass::FrameDebugSnapshot snapshot = Bridge.getTxDebugSnapshot();
    printSnapshot(snapshot);
    clearSnapshotStats();
  }
#else
  // Manual mode: watch for commands on the USB serial console so the sketch
  // stays silent unless explicitly triggered.
  if (Serial.available()) {
    char cmd = static_cast<char>(Serial.read());
    switch (cmd) {
      case 'f':
      case 'F':
        Serial.println(F("[FrameDebug] Manual CommandId::CMD_GET_FREE_MEMORY trigger"));
        Bridge.requestGetFreeMemory();
        delay(20);
        printSnapshot(Bridge.getTxDebugSnapshot());
        clearSnapshotStats();
        break;
      case 's':
      case 'S':
        printSnapshot(Bridge.getTxDebugSnapshot());
        break;
      case 'c':
      case 'C':
        clearSnapshotStats();
        break;
      case '\\n':
      case '\\r':
        break;
      default:
        Serial.print(F("[FrameDebug] Unknown command '"));
        Serial.print(cmd);
        Serial.println(F("'. Use f=free-mem, s=snapshot, c=clear."));
        break;
    }
  }
#endif  // FRAME_DEBUG_AUTO_POLL
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