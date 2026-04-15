#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "test_constants.h"
#include "test_support.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"

// Define the global delegates and stubs for HardwareSerial stub
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;

// Unity setup/teardown
void setUp(void) {}
void tearDown(void) {}

using namespace bridge::test;

void reset_bridge_comp(BiStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "top-secret");
  auto& ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  ba.setSynchronized();
}

void test_all_handlers_coverage() {
  BiStream stream;
  reset_bridge_comp(stream);
  
  rpc::Frame frame = {};
  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  TestAccessor::create(Bridge).dispatch(frame);
  
  frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
  TestAccessor::create(Bridge).dispatch(frame);

  frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
  TestAccessor::create(Bridge).dispatch(frame);
}

void test_process_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  
  #if BRIDGE_ENABLE_PROCESS
  Process.reset();
  #endif
}

void test_console_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  Console.begin();
  Console.write('A');
}

void test_datastore_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  #if BRIDGE_ENABLE_DATASTORE
  // No begin needed
  #endif
}

void test_mailbox_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  #if BRIDGE_ENABLE_MAILBOX
  // No begin needed
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
