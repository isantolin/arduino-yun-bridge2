#define BRIDGE_ENABLE_TEST_INTERFACE
#include <etl/array.h>
#include <etl/span.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "services/Console.h"
#include "test_support.h"  // IWYU pragma: keep

using namespace bridge::test;

void setUp() {}
void tearDown() {}

void reset_bridge() {
  Bridge.begin(115200, "6368616e67656d65313233");
  Console.begin();
}

void test_bridge_initialization() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());
  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_handshake() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // 1. Prepare Handshake Payload using computeHandshakeTag
  etl::array<uint8_t, 16> nonce;
  nonce.fill(0x42);
  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), nonce.size(), tag.data());

  rpc::payload::LinkSync msg = {};
  etl::copy_n(nonce.begin(), 16, msg.nonce.bytes);
  msg.nonce.size = 16;
  etl::copy_n(tag.begin(), 16, msg.tag.bytes);
  msg.tag.size = 16;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  (void)pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg);

  // 2. Build LinkSync frame using FrameBuilder
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};
  // [MEM-SAVE] Reusing nonce for handshake (aligned with protocol spec).
  etl::copy_n(nonce.begin(), rpc::AEAD_NONCE_SIZE, frame_nonce.begin());

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 1,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, tag),
      frame_raw);

  // 3. Dispatch using FrameParser
  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();

  TEST_ASSERT(ba.isSynchronized());
}

void test_bridge_send_frame() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> payload = {0xAA, 0xBB};
  TEST_ASSERT(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 123,
                               etl::span<const uint8_t>(payload.data(), 2)));
}

void test_bridge_process_rx() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::payload::DigitalWrite msg = {};
  msg.pin = 13;
  msg.value = 1;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  (void)pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg);

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 10,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, {}),
      frame_raw);

  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();
}

void test_bridge_dedup_console_write() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Build ConsoleWrite frame once
  rpc::payload::ConsoleWrite msg = {};
  const char* text = "TEST";
  etl::copy_n(text, 4, msg.data.bytes);
  msg.data.size = 4;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  (void)pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg);

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), 55,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, {}),
      frame_raw);

  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());

  // 2. Dispatch twice
  ba.dispatch(frame_res.value());
  Bridge.process();
  TEST_ASSERT_EQUAL(4, Console.available());

  ba.dispatch(frame_res.value());
  Bridge.process();
  // 3. Verify Console.available() remains consistent (deduplicated)
  TEST_ASSERT_EQUAL(4, Console.available());
}

void test_bridge_status_ack() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Trigger a command that requires ACK
  (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 77);
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  // 2. Build STATUS_ACK frame targeting sequence ID 77
  rpc::payload::AckPacket p = {};
  p.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  (void)pb_encode(&pbos, rpc::Payload::get_fields<decltype(p)>(), &p);

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> frame_tag = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::StatusCode::STATUS_ACK), 77,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, frame_tag),
      frame_raw);

  // 3. Dispatch and verify isAwaitingAck() becomes false
  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();

  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_post_and_stack_sentinel() {
  TEST_ASSERT_TRUE(bridge::hal::run_power_on_self_tests());
  TEST_ASSERT_TRUE(bridge::hal::checkStackOverflow());
  TEST_ASSERT_GREATER_OR_EQUAL_UINT16(bridge::hal::MIN_STACK_MARGIN_BYTES,
                                      bridge::hal::getFreeStackMargin());
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
  TEST_ASSERT_GREATER_OR_EQUAL_UINT16(bridge::hal::MIN_STACK_MARGIN_BYTES,
                                      Bridge.getFreeStackMargin());
}

void test_bridge_wcet_tracking() {
  Bridge.resetWcetStats();
  TEST_ASSERT_EQUAL_UINT32(0, Bridge.getWcetMaxMicros());
  Bridge.process();
  TEST_ASSERT_GREATER_OR_EQUAL_UINT32(0, Bridge.getWcetMaxMicros());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_initialization);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  RUN_TEST(test_bridge_dedup_console_write);
  RUN_TEST(test_bridge_status_ack);
  RUN_TEST(test_bridge_post_and_stack_sentinel);
  RUN_TEST(test_bridge_wcet_tracking);
  return UNITY_END();
}
