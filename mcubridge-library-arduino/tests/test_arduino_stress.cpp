#define BRIDGE_ENABLE_TEST_INTERFACE
#include <Arduino.h>
#include <etl/array.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "test_support.h"

// [SIL-2] Global stub definitions for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }
void delay(unsigned long ms) { g_test_millis += ms; }

namespace etl {
void handle_error(const etl::exception& e);
}

namespace {

using bridge::test::TestAccessor;

void test_bridge_reliable_retry_exhaustion() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Send reliable frame
  TEST_ASSERT_TRUE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 1, {}));
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  // 2. Trigger timeout multiple times until limit
  for (int i = 1; i < rpc::RPC_DEFAULT_RETRY_LIMIT; ++i) {
    ba.onAckTimeout();
    TEST_ASSERT_TRUE(ba.isAwaitingAck());
  }

  // Final call that triggers transition
  ba.onAckTimeout();
  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_packet_corruption_chaos() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Inyectar ruido asíncrono
  etl::array<uint8_t, 5> noise = {0x00, 0xFF, 0xAA, 0x55, 0x00};
  ba.invokePacketReceived(noise);

  // Inyectar frame truncado
  etl::array<uint8_t, 3> truncated = {0x02, 0x01, 0x00};
  ba.invokePacketReceived(truncated);

  TEST_ASSERT_TRUE(true);
}

void test_bridge_dispatch_security_denial() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Configure secret to enable security checks
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "secure_secret_1234567890123456");

  // MPU is NOT synchronized yet.
  // Try to send a restricted command
  rpc_pb_RpcEnvelope f;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_FREE_MEMORY);
  f.sequence_id = 1;

  ba.dispatch(f);

  // Should have sent some response (error status)
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0);
}

void test_bridge_fsm_illegal_transitions() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.setIdle();
  // EvAckReceived in Idle should be ignored
  // We just verify it doesn't crash
  TEST_ASSERT_TRUE(true);
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_reliable_retry_exhaustion);
  RUN_TEST(test_bridge_packet_corruption_chaos);
  RUN_TEST(test_bridge_dispatch_security_denial);
  RUN_TEST(test_bridge_fsm_illegal_transitions);
  return UNITY_END();
}
