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
  auto ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());
  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_handshake() {
  reset_bridge();
  auto ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // 1. Prepare Handshake Payload
  etl::array<uint8_t, 16> nonce;
  nonce.fill(0x42);
  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), nonce.size(), tag.data());

  rpc_pb_LinkSync msg = rpc_pb_LinkSync_init_default;
  etl::copy_n(nonce.begin(), 16, msg.nonce.bytes);
  msg.nonce.size = 16;
  etl::copy_n(tag.begin(), 16, msg.tag.bytes);
  msg.tag.size = 16;

  rpc_pb_RpcPayload payload = rpc_pb_RpcPayload_init_default;
  payload.which_msg = rpc_pb_RpcPayload_link_sync_tag;
  payload.msg.link_sync = msg;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(pb_encode(&pbos, rpc_pb_RpcPayload_fields, &payload));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> frame_nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> frame_tag = {};

  size_t len = rpc::FrameBuilder::build(
      frame_raw, 1,
      etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
      frame_nonce, frame_tag);

  // 2. Dispatch
  ba.invokePacketReceived(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();

  TEST_ASSERT(ba.isSynchronized());
}

void test_bridge_send_frame() {
  reset_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_ConsoleWrite msg = rpc_pb_ConsoleWrite_init_default;
  etl::copy_n("HI", 2, msg.data.bytes);
  msg.data.size = 2;

  TEST_ASSERT(Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 123, msg));
}

void test_bridge_process_rx() {
  reset_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_DigitalWrite msg = rpc_pb_DigitalWrite_init_default;
  msg.pin = 13;
  msg.value = 1;

  rpc::Frame frame;
  set_pb_payload(frame, msg, rpc_pb_RpcPayload_digital_write_tag);
  frame.envelope.sequence_id = 10;

  ba.dispatch(frame);
  Bridge.process();

  // Verify behavior (mocked or observed via status)
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_initialization);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  return UNITY_END();
}
