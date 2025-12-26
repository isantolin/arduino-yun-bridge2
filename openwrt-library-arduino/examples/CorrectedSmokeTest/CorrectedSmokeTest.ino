/*
  Corrected Smoke Test for Yun Bridge 2.0
  Waits for Console connection to avoid spamming the bus before handshake.
*/

#include <Bridge.h>
// Console is already declared in Bridge.h in this library version
// #include <Console.h> 

// Secret from UCI (yunbridge.general.serial_shared_secret)
// Must match the daemon's configuration to pass the handshake.
#define BRIDGE_SERIAL_SHARED_SECRET "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"

void setup() {
  // Initialize Bridge with the shared secret
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);
  
  // Initialize Console
  Console.begin();
  
  pinMode(13, OUTPUT);

  // CRITICAL: Wait for Console to be ready (Linux handshake complete)
  // This prevents "Rejecting MCU frame before link synchronisation" errors.
  // We must call Bridge.process() to handle the handshake frames!
  // Use non-blocking blink to ensure we process serial data as fast as possible.
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

  Console.println("MCU: Setup complete. Handshake verified.");
}

void loop() {
  // CRITICAL: Must call process() frequently to handle incoming commands (heartbeats, RPC)
  Bridge.process();

  // Removed debug print to prevent serial collisions with RPC protocol
  /*
  static long lastPrint = 0;
  if (millis() - lastPrint > 1000) {
    lastPrint = millis();
    Console.println("Estado: 0x05 (Running)");
  }
  */
}
