#include <unity.h>
#include <etl/array.h>
#include <etl/span.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "BridgeTestHelper.h"
#include "services/Console.h"

using namespace bridge::test;

void setUp() {}
void tearDown() {}

void reset_bridge() {
  Bridge.begin(115200, "6368616e67656d65313233");
  Console.begin();
}

void test_bridge_initialization() {
  reset_bridge();
  Console.begin();
  auto ba = TestAccessor::create(Bridge);
  ba.clearSharedSecret();
  TEST_ASSERT_FALSE(ba.isSynchronized());
  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_handshake() {
  reset_bridge();
  Console.begin();
  auto ba = TestAccessor::create(Bridge);
  ba.clearSharedSecret();
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // 1. Prepare Handshake Payload using computeHandshakeTag
  etl::array<uint8_t, 16> nonce;
  nonce.fill(0x42);
  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), nonce.size(), tag.data());

  rpc_pb_LinkSync msg = {};
  etl::copy_n(nonce.begin(), 16, msg.nonce);
  
  etl::copy_n(tag.begin(), 16, msg.tag);
  

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  rpc_pb_McuFrame mf = rpc_pb_McuFrame_init_default;
  mf.which_message = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  mf.seq_id = 5;
  mf.message.link_sync = msg;
  TEST_ASSERT_TRUE(pb_encode(&pbos, rpc_pb_McuFrame_fields, &mf));

  // 2. Build rpc_pb_LinkSync frame using FrameBuilder
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};
  // [MEM-SAVE] Reusing nonce for handshake (aligned with protocol spec).
  etl::copy_n(nonce.begin(), rpc::AEAD_NONCE_SIZE, frame_nonce.begin());

  size_t len = rpc::FrameBuilder::build(
      frame_raw, rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 1,
      etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
      frame_nonce, tag);

  // 3. Dispatch using FrameParser
  auto frame_res = rpc::FrameParser().parse(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();

  TEST_ASSERT(ba.isSynchronized());
}

void test_bridge_send_frame() {
  reset_bridge();
  Console.begin();
  auto ba = TestAccessor::create(Bridge);
  ba.clearSharedSecret();
  ba.setSynchronized();

  etl::array<uint8_t, 2> payload = {0xAA, 0xBB};
  TEST_ASSERT(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 123,
                               etl::span<const uint8_t>(payload.data(), 2)));
}

void test_bridge_process_rx() {
  reset_bridge();
  Console.begin();
  auto ba = TestAccessor::create(Bridge);
  ba.clearSharedSecret();
  ba.setSynchronized();

  rpc_pb_DigitalWrite msg = {};
  msg.pin = 13;
  msg.value = 1;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  rpc_pb_McuFrame mf = rpc_pb_McuFrame_init_default;
  mf.which_message = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  mf.seq_id = 6;
  mf.message.digital_write = msg;
  TEST_ASSERT_TRUE(pb_encode(&pbos, rpc_pb_McuFrame_fields, &mf));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> frame_tag = {};

  size_t len = rpc::FrameBuilder::build(
      frame_raw, rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 10,
      etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
      frame_nonce, frame_tag);

  auto frame_res = rpc::FrameParser().parse(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();
}

void test_bridge_dedup_console_write() {
  reset_bridge();
  Console.begin();
  auto ba = TestAccessor::create(Bridge);
  ba.clearSharedSecret();
  ba.setSynchronized();

  // 1. Build rpc_pb_ConsoleWrite frame once
  rpc_pb_ConsoleWrite msg = {};
  const char* text = "TEST";
  etl::copy_n(text, 4, msg.data.bytes);
  msg.data.size = 4;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  rpc_pb_McuFrame mf = rpc_pb_McuFrame_init_default;
  mf.which_message = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  mf.seq_id = 7;
  mf.message.console_write = msg;
  TEST_ASSERT_TRUE(pb_encode(&pbos, rpc_pb_McuFrame_fields, &mf));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> frame_tag = {};

  size_t len = rpc::FrameBuilder::build(
      frame_raw, rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), 55,
      etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
      frame_nonce, frame_tag);

  auto frame_res = rpc::FrameParser().parse(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());

  // 2. Dispatch twice
  ba.dispatch(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();
  TEST_ASSERT_EQUAL(96, ba.getLastCommandId()); TEST_ASSERT_EQUAL(4, Console.available());

  ba.dispatch(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();
  // 3. Verify Console.available() remains consistent (deduplicated)
  TEST_ASSERT_EQUAL(96, ba.getLastCommandId()); TEST_ASSERT_EQUAL(4, Console.available());
}

void test_bridge_status_ack() {
  reset_bridge();
  Console.begin();
  auto ba = TestAccessor::create(Bridge);
  ba.clearSharedSecret();
  ba.setSynchronized();

  // 1. Trigger a command that requires ACK
  (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 77);
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  // 2. Build STATUS_ACK frame targeting sequence ID 77
  rpc_pb_AckPacket p = {};
  p.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  rpc_pb_McuFrame mf = rpc_pb_McuFrame_init_default; mf.which_message = rpc::to_underlying(rpc::CommandId::CMD_ACK); mf.seq_id = 4; mf.message.ack = p; TEST_ASSERT_TRUE(pb_encode(&pbos, rpc_pb_McuFrame_fields, &mf));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> frame_tag = {};

  size_t len = rpc::FrameBuilder::build(
      frame_raw, rpc::to_underlying(rpc::StatusCode::STATUS_ACK), 77,
      etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
      frame_nonce, frame_tag);

  // 3. Dispatch and verify isAwaitingAck() becomes false
  auto frame_res = rpc::FrameParser().parse(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();

  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_initialization);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  RUN_TEST(test_bridge_dedup_console_write);
  RUN_TEST(test_bridge_status_ack);
  return UNITY_END();
}
