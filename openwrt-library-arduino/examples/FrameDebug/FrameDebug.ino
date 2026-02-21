// [SIL-2] Use centralized bridge_config.h for consistency.
// Do not override layout macros here to avoid ODR violations.
#define BRIDGE_SECRET "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"

// When set to 1 the sketch automatically sends CommandId::CMD_GET_FREE_MEMORY_RESP frames
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
unsigned long last_send_ms = 0;

void printSnapshot(const BridgeClass::FrameDebugSnapshot &snapshot) {
  // Use Console.print instead of Serial.print to reuse the bridge channel safely
  // if available, or fallback to Serial only if explicitly debugging local USB.
  // For FrameDebug, we assume local USB debugging via Serial is intended.
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
  Serial.begin(rpc::RPC_DEFAULT_BAUDRATE);
  // NOTE: Removed blocking wait for Serial to allow daemon handshake
  // independent of USB connection.
  /*
  while (!Serial) {
    // Wait for the USB serial console to be ready.
  }
  */

  Serial.println(F("[FrameDebug] Starting"));

  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SECRET);
  Serial.println(F("[FrameDebug] Bridge initialized with sketch-defined secret"));

  // Wait for handshake with non-blocking LED blink
  pinMode(13, OUTPUT);
  long lastBlink = 0;
  bool ledState = false;
  
  // Non-blocking sync wait
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
  if (now - last_send_ms >= 5000UL) {
    last_send_ms = now;

    // Directly emit CMD_GET_FREE_MEMORY_RESP to exercise the TX path.
    Serial.println(F("[FrameDebug] Sending CommandId::CMD_GET_FREE_MEMORY_RESP"));
    uint16_t free_mem = getFreeMemory();
    uint8_t resp[2];
    rpc::write_u16_be(resp, free_mem);
    Bridge.sendFrame(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, resp, sizeof(resp));

    Bridge.flushStream();  // [SIL-2] Non-blocking flush replaces delay(20).

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
        {
          Serial.println(F("[FrameDebug] Manual CommandId::CMD_GET_FREE_MEMORY_RESP trigger"));
          uint16_t fm = getFreeMemory();
          uint8_t rp[2];
          rpc::write_u16_be(rp, fm);
          Bridge.sendFrame(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, rp, sizeof(rp));
          Bridge.flushStream();  // [SIL-2] Non-blocking flush.
          printSnapshot(Bridge.getTxDebugSnapshot());
          clearSnapshotStats();
        }
        break;
      case 's':
      case 'S':
        printSnapshot(Bridge.getTxDebugSnapshot());
        break;
      case 'c':
      case 'C':
        clearSnapshotStats();
        break;
      case '\n':
      case '\r':
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
