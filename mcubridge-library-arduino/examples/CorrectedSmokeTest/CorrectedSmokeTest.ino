/*
 * CorrectedSmokeTest.ino - Full-stack E2E Verification
 */
#include <Bridge.h>
#include <protocol/rpc_services.h>

#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

void setup() {
  pinMode(13, OUTPUT); digitalWrite(13, LOW);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);
  const uint32_t sync_deadline = millis() + bridge::config::SYNC_TIMEOUT_MS;
  while (!Bridge.isSynchronized()) {
    if (static_cast<int32_t>(millis() - sync_deadline) > 0) { Bridge.enterSafeState(); break; }
    Bridge.process();
  }
  digitalWrite(13, HIGH);
}

void loop() {
  Bridge.process();
  while (rpc::services::console::available()) {
    int c = rpc::services::console::read();
    if (c >= 0) rpc::services::console::write(static_cast<uint8_t>(c));
  }
}
