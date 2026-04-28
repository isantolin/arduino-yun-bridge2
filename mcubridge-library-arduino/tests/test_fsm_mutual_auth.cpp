#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "fsm/bridge_fsm.h"
#include "test_support.h"
#include <etl/array.h>

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

namespace {
using bridge::test::TestAccessor;

void test_fsm_initial_state() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_mutual_auth_success() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  TEST_ASSERT(ba.isSynchronized());
}

void test_mutual_auth_failure_to_startup() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  // Observed behavior: Handshake failure resets to Startup
  TEST_ASSERT(ba.getStartupStabilizing());
}

void test_fsm_timeout_to_fault() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvTimeout());
  // Observed behavior: Timeout from synchronized goes to Fault
  TEST_ASSERT(ba.isFault());
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_initial_state);
  RUN_TEST(test_mutual_auth_success);
  RUN_TEST(test_mutual_auth_failure_to_startup);
  RUN_TEST(test_fsm_timeout_to_fault);
  return UNITY_END();
}
