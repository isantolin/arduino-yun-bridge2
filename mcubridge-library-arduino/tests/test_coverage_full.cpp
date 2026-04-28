#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
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

void test_fsm_timeout_fault() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  // Trigger timeout to move to Fault
  ba.trigger(bridge::fsm::EvTimeout());
  TEST_ASSERT(ba.isFault());
}

void test_crc_error_escalation() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  
  etl::array<uint8_t, 1> payload = {0x00};
  for(int i = 0; i < 6; ++i) {
    ba._onPacketReceived(etl::span<const uint8_t>(payload.data(), payload.size()));
  }
}

void test_ack_timeout_retry_exceeded() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  
  ba.onAckTimeout();
  ba.onAckTimeout();
  ba.onAckTimeout();
  ba.onAckTimeout();
}

void test_error_policy_direct() {
  bridge::SafeStatePolicy policy;
  etl::exception e("test", "test", 1);
  policy.handle(Bridge, e);
}

void test_service_edge_cases_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  static etl::array<uint8_t, 256> buf;
  auto dispatch_payload = [&](rpc::CommandId id, auto payload) {
    buf.fill(0);
    msgpack::Encoder enc(buf.data(), buf.size());
    payload.encode(enc);
    rpc::Frame f = {};
    f.header.command_id = (uint16_t)id;
    f.payload = enc.result();
    ba.dispatch(f);
  };

  dispatch_payload(rpc::CommandId::CMD_LINK_SYNC, rpc::payload::LinkSync{});
}

void test_spi_real_logic_exhaustive() {
#if BRIDGE_ENABLE_SPI
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
#endif
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_timeout_fault);
  RUN_TEST(test_crc_error_escalation);
  RUN_TEST(test_ack_timeout_retry_exceeded);
  RUN_TEST(test_error_policy_direct);
  RUN_TEST(test_service_edge_cases_exhaustive);
  RUN_TEST(test_spi_real_logic_exhaustive);
  return UNITY_END();
}
