#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <Arduino.h>
#include <etl/array.h>
#include <unity.h>

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

void test_bridge_handshake_timeout() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Trigger handshake timeout before sync
  ba.onHandshakeTimeout();
  TEST_ASSERT_TRUE(ba.isFault());
  TEST_ASSERT_FALSE(ba.isTxEnabled());
}

void test_bridge_packet_corruption_chaos() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Inject asynchronous random noise into HDLC decoder
  const uint8_t noise[] = {0x00, 0xFF, 0xAA, 0x55, 0x7E, 0x12, 0x34};
  ba.feedHdlcBytes(etl::span<const uint8_t>(noise, sizeof(noise)));

  // Inject truncated frame with valid flag start but cut off
  const uint8_t truncated[] = {0x7E, 0x01, 0x00, 0x7E};
  ba.feedHdlcBytes(etl::span<const uint8_t>(truncated, sizeof(truncated)));

  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_bridge_dispatch_security_denial() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Configure secret to enable security checks
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "secure_secret_1234567890123456");

  // MPU is NOT synchronized yet.
  // Try to send a restricted command (GetFreeMemory) before mutual auth
  // handshake
  etl::array<uint8_t, 256> frame_buf;
  size_t len =
      build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kRequest,
                           pw::tokenizer::Hash("bridge.SystemService"),
                           pw::tokenizer::Hash("GetFreeMemory"),
                           etl::span<const uint8_t>(), frame_buf, 1, 1);
  TEST_ASSERT_TRUE(len > 0);

  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), len));

  // Must not synchronize and TX must remain disabled
  TEST_ASSERT_FALSE(ba.isSynchronized());
  TEST_ASSERT_FALSE(ba.isTxEnabled());
}

void test_bridge_fsm_illegal_transitions() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.setIdle();
  ba.trigger(bridge::fsm::EvBaudrateApplied());
  TEST_ASSERT_TRUE(ba.isUnsynchronized());
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_handshake_timeout);
  RUN_TEST(test_bridge_packet_corruption_chaos);
  RUN_TEST(test_bridge_dispatch_security_denial);
  RUN_TEST(test_bridge_fsm_illegal_transitions);
  return UNITY_END();
}
