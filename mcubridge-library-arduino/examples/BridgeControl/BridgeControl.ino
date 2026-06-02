/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 */

#include <Bridge.h>
#include <protocol/rpc_services.h>
#include <string.h>

#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

void on_rpc_command(const rpc_pb_RpcEnvelope& envelope) {
  rpc::services::console::write((const uint8_t*)"Unk cmd", 7);
}

void on_bridge_status(rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
  (void)payload; (void)status_code;
}

void setup() {
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  Bridge.onCommand(BridgeClass::CommandHandler::create<on_rpc_command>());
  Bridge.onStatus(BridgeClass::StatusHandler::create<on_bridge_status>());

  const uint32_t sync_deadline = millis() + bridge::config::SYNC_TIMEOUT_MS;
  while (!Bridge.isSynchronized()) {
    if (static_cast<int32_t>(millis() - sync_deadline) > 0) { Bridge.enterSafeState(); break; }
    Bridge.process();
  }
}

void loop() {
  Bridge.process();
  static unsigned long lastMailboxCheck = 0;
  if (millis() - lastMailboxCheck > 500) {
    lastMailboxCheck = millis();
#if BRIDGE_ENABLE_MAILBOX
    rpc::services::mailbox::requestRead();
#endif
  }
}
