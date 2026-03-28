/*
 * CorrectedSmokeTest.ino - Full-stack E2E Verification
 */
#include <Bridge.h>

// Must match the daemon's configuration to pass the handshake.
#define BRIDGE_SERIAL_SHARED_SECRET \
  "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"

void setup() {
  // [SIL-2] Force safe state for actuators before enabling interrupts or protocol
  pinMode(13, OUTPUT);
  digitalWrite(13, LOW);

  // Initialize Bridge with the shared secret
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  // Initialize Console
  Console.begin();

  // CRITICAL: Wait for Console to be ready (Linux handshake complete)
  // This prevents "Rejecting MCU frame before link synchronisation" errors.
  // We must call Bridge.process() to handle the handshake frames!
  // Use non-blocking blink to ensure we process serial data as fast as
  // possible.
  long lastBlink = 0;
  bool ledState = false;
  while (!Console.isReady()) {
    Bridge.process();
    if (millis() - lastBlink > 100) {
      lastBlink = millis();
      ledState = !ledState;
      digitalWrite(13, ledState ? HIGH : LOW);
    }
  }

  // Removed Console.println to avoid mixing console data with protocol frames
  // Console messages can interfere with COBS framing
  digitalWrite(13, HIGH);  // Indicate handshake complete
}

void loop() {
  // Main processing loop
  Bridge.process();

  // Example: Echo serial console input back to verify bi-directional link
  while (Console.available()) {
    int c = Console.read();
    if (c >= 0) {
      Console.write(static_cast<uint8_t>(c));
    }
  }

  /*
  static long lastPrint = 0;
  if (millis() - lastPrint > 1000) {
    lastPrint = millis();
    Console.println("Estado: 0x05 (TIMEOUT)");
  }
  */
}
