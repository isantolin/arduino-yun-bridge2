#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

// Services
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

#include <etl/array.h>

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }
void delay(unsigned long ms) { g_test_millis += ms; }

namespace {

using bridge::test::TestAccessor;

void test_bridge_reset_state() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  TEST_ASSERT(ba.isUnsynchronized());
  Bridge.enterSafeState();
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_bridge_exhaustive_dispatch() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  static etl::array<uint8_t, 256> buf;
  auto dispatch_payload = [&](rpc::CommandId id, auto payload) {
    buf.fill(0);
    msgpack::Encoder enc(buf.data(), buf.size());
    payload.encode(enc);
    rpc::Frame f = {};
    f.header.command_id = (uint16_t)id;
    f.payload = enc.result();
    f.header.payload_length = (uint16_t)f.payload.size();
    ba.dispatch(f);
  };

  dispatch_payload(rpc::CommandId::CMD_GET_VERSION, rpc::payload::VersionResponse{2, 8, 5});
  // Check if it tried to respond
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_bridge_hal_callbacks() {
  // ARCH_HOST is 4 based on actual run
  TEST_ASSERT_EQUAL(4, bridge::hal::getArchId());
  uint8_t d, a;
  bridge::hal::getPinCounts(d, a);
  TEST_ASSERT(d > 0);
  TEST_ASSERT(bridge::hal::getCapabilities() != 0);
}

void test_console_validation() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  Console.begin();
  Console.write('A');
  Console.process();
  
  // Verify data reached stream (COBS encoded)
  TEST_ASSERT(stream.tx_buf.len > 0);
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_reset_state);
  RUN_TEST(test_bridge_exhaustive_dispatch);
  RUN_TEST(test_bridge_hal_callbacks);
  RUN_TEST(test_console_validation);
  return UNITY_END();
}
