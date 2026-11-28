#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <Arduino.h>
#include <etl/array.h>
#include "pw_unit_test/framework.h"

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "test_support.h"

// [SIL-2] Global stub definitions for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }
void delay(unsigned long ms) { g_test_millis += ms; }

namespace etl {
void handle_error(const etl::exception& e);
}

namespace {

using bridge::test::TestAccessor;

TEST(ArduinoStress, BridgeHandshakeTimeout) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Trigger handshake timeout before sync
  ba.onHandshakeTimeout();
  EXPECT_TRUE(ba.isFault());
  EXPECT_FALSE(ba.isTxEnabled());
}

TEST(ArduinoStress, BridgePacketCorruptionChaos) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Inject asynchronous random noise into HDLC decoder
  const uint8_t noise[] = {0x00, 0xFF, 0xAA, 0x55, 0x7E, 0x12, 0x34};
  ba.feedHdlcBytes(pw::span<const uint8_t>(noise, sizeof(noise)));

  // Inject truncated frame with valid flag start but cut off
  const uint8_t truncated[] = {0x7E, 0x01, 0x00, 0x7E};
  ba.feedHdlcBytes(pw::span<const uint8_t>(truncated, sizeof(truncated)));

  EXPECT_FALSE(Bridge.isSynchronized());
}

TEST(ArduinoStress, BridgeDispatchSecurityDenial) {
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
      build_hdlc_rpc_frame(1, pw::rpc::internal::pwpb::PacketType::REQUEST,
                           pw::tokenizer::Hash("rpc.pb.SystemService"),
                           pw::tokenizer::Hash("GetFreeMemory"),
                           pw::span<const uint8_t>(), frame_buf, 1, 1);
  EXPECT_TRUE(len > 0);

  ba.feedHdlcBytes(pw::span<const uint8_t>(frame_buf.data(), len));

  // Must not synchronize and TX must remain disabled
  EXPECT_FALSE(ba.isSynchronized());
  EXPECT_FALSE(ba.isTxEnabled());
}

TEST(ArduinoStress, BridgeFsmIllegalTransitions) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.setIdle();
  ba.trigger(bridge::fsm::EvBaudrateApplied());
  EXPECT_TRUE(ba.isUnsynchronized());
}

}  // namespace

