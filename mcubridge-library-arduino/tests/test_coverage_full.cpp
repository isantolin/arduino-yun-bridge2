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

void test_fsm_timeout_fault() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);

  ba.setIdle();
  ba.onStartupStabilized();
  ba.setSynchronized();

  TEST_ASSERT(localBridge.isSynchronized());
  
  ba.forceTimeout(); // Simular un fallo fatal a través de timeout
  
  TEST_ASSERT(ba.isFault());
  TEST_ASSERT(!localBridge.isSynchronized());
}

void test_crc_error_escalation() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);

  ba.setIdle();
  ba.onStartupStabilized();
  ba.setLastParseError(rpc::FrameError::CRC_MISMATCH);
  TEST_ASSERT(ba.getLastParseError() == rpc::FrameError::CRC_MISMATCH);
}

void test_ack_timeout_retry_exceeded() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);

  ba.setIdle();
  ba.onStartupStabilized();
  ba.setSynchronized();

  uint8_t payload[] = {0x00};
  (void)localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 0, etl::span<const uint8_t>(payload, 1));
  
  TEST_ASSERT(ba.isAwaitingAck());

  // Set retry count to limit
  ba.setRetryCount(ba.getAckRetryLimit());

  // Trigger timeout which should fault / reset
  ba.onAckTimeout();
  
  TEST_ASSERT(ba.isFault());
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_timeout_fault);
  RUN_TEST(test_crc_error_escalation);
  RUN_TEST(test_ack_timeout_retry_exceeded);
  return UNITY_END();
}
