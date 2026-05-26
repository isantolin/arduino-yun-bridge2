#include <unity.h>
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "BridgeTestHelper.h"

using namespace bridge::test;

void setUp() {}
void tearDown() {}

void test_integrated_handshake_to_command() {
  Bridge.begin(115200, "6368616e67656d65313233");
  auto ba = TestAccessor::create(Bridge);

  // 1. Handshake
  etl::array<uint8_t, 16> nonce; nonce.fill(0x42);
  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), nonce.size(), tag.data());

  rpc_pb_LinkSync sync = rpc_pb_LinkSync_init_default;
  etl::copy_n(nonce.begin(), 16, sync.nonce.bytes);
  sync.nonce.size = 16;
  etl::copy_n(tag.begin(), 16, sync.tag.bytes);
  sync.tag.size = 16;

  rpc_pb_RpcPayload sync_pl = rpc_pb_RpcPayload_init_default;
  sync_pl.which_msg = rpc_pb_RpcPayload_link_sync_tag;
  sync_pl.msg.link_sync = sync;

  etl::array<uint8_t, 128> buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(buf.data(), buf.size());
  pb_encode(&pbos, rpc_pb_RpcPayload_fields, &sync_pl);

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, 12> frame_nonce = {};
  etl::array<uint8_t, 16> frame_tag = {};
  size_t len = rpc::FrameBuilder::build(frame_raw, 1, etl::span<const uint8_t>(buf.data(), pbos.bytes_written), frame_nonce, frame_tag);

  ba.invokePacketReceived(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();
  TEST_ASSERT(ba.isSynchronized());

  // 2. Encrypted Command (Digital Write)
  rpc_pb_DigitalWrite dw = rpc_pb_DigitalWrite_init_default;
  dw.pin = 13;
  dw.value = 1;

  rpc_pb_RpcPayload dw_pl = rpc_pb_RpcPayload_init_default;
  dw_pl.which_msg = rpc_pb_RpcPayload_digital_write_tag;
  dw_pl.msg.digital_write = dw;

  pbos = pb_ostream_from_buffer(buf.data(), buf.size());
  pb_encode(&pbos, rpc_pb_RpcPayload_fields, &dw_pl);

  // Encrypt
  etl::array<uint8_t, 128> enc_pl;
  uint64_t n_counter = 0;
  ba.encryptFrame(10, etl::span<const uint8_t>(buf.data(), pbos.bytes_written), frame_nonce, frame_tag, enc_pl, &n_counter);

  len = rpc::FrameBuilder::build(frame_raw, 10, etl::span<const uint8_t>(enc_pl.data(), pbos.bytes_written), frame_nonce, frame_tag);
  ba.invokePacketReceived(etl::span<const uint8_t>(frame_raw.data(), len));
  Bridge.process();
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_integrated_handshake_to_command);
  return UNITY_END();
}
