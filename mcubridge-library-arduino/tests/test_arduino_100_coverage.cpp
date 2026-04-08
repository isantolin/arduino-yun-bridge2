#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }
void delay(unsigned long ms) { g_test_millis += ms; }

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using bridge::test::TestAccessor;

void test_bridge_reset_state() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto ba = TestAccessor::create(localBridge);

  ba.onStartupStabilized(); // Move from STARTUP to UNSYNCHRONIZED
  TEST_ASSERT(ba.isUnsynchronized());

  localBridge.enterSafeState();
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_bridge_is_recent_duplicate_edge_cases() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin();
  auto ba = TestAccessor::create(localBridge);

  rpc::Frame f = {};
  f.header.sequence_id = 42;

  ba.clearRxHistory();
  TEST_ASSERT(!ba.isRecentDuplicateRx(f));

  ba.markRxProcessed(f);

  // Debería ser un duplicado reciente
  TEST_ASSERT(ba.isRecentDuplicateRx(f));

  // Frame diferente
  rpc::Frame f2 = {};
  f2.header.sequence_id = 43;
  TEST_ASSERT(!ba.isRecentDuplicateRx(f2));

  // Llenar el historial para que ruede (buffer de 16 por defecto)
  for (int i = 0; i < 30; i++) {
    rpc::Frame temp = {};
    temp.header.sequence_id = static_cast<uint16_t>(100 + i);
    ba.markRxProcessed(temp);
  }

  // Ahora '42' ya no debería estar en el historial
  TEST_ASSERT(!ba.isRecentDuplicateRx(f));
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_reset_state);
  RUN_TEST(test_bridge_is_recent_duplicate_edge_cases);
  return UNITY_END();
}
