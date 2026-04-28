#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "Bridge.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "test_constants.h"
#include "test_support.h"

// Define the global delegates and stubs for HardwareSerial stub
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;

// Unity setup/teardown
void setUp(void) {}
void tearDown(void) {}

void reset_bridge_comp(BiStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "top-secret");
  simulate_handshake(Bridge, stream);
}

void test_all_handlers_coverage() {
  BiStream stream;
  reset_bridge_comp(stream);
  
  stream.feed_frame(rpc::CommandId::CMD_GET_VERSION, 10, {});
  Bridge.process();
  
  stream.feed_frame(rpc::CommandId::CMD_GET_FREE_MEMORY, 11, {});
  Bridge.process();

  stream.feed_frame(rpc::CommandId::CMD_GET_CAPABILITIES, 12, {});
  Bridge.process();
}

void test_process_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  
  #if BRIDGE_ENABLE_PROCESS
  Process.kill(99);
  #endif
}

void test_console_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  Console.begin();
  Console.write('A');
  Console.process();
}

void test_datastore_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  #if BRIDGE_ENABLE_DATASTORE
  uint8_t v[] = {0};
  DataStore.set("test", etl::span<const uint8_t>(v, 1));
  #endif
}

void test_mailbox_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  #if BRIDGE_ENABLE_MAILBOX
  Mailbox.requestRead();
  #endif
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_all_handlers_coverage);
  RUN_TEST(test_process_api);
  RUN_TEST(test_console_api);
  RUN_TEST(test_datastore_api);
  RUN_TEST(test_mailbox_api);
  return UNITY_END();
}
