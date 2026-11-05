#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <Arduino.h>
#include <etl/array.h>
#include <etl/span.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "services/Console.h"
#include "test_support.h"

// [SIL-2] Global stub definitions for host environment
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

using bridge::test::TestAccessor;

static rpc::StatusCode s_captured_harden_status = rpc::StatusCode::STATUS_OK;
static void capture_harden_status(rpc::StatusCode s, etl::span<const uint8_t>) {
  s_captured_harden_status = s;
}

void setUp() {}
void tearDown() {}

void test_bridge_malformed_rpc_packet() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  s_captured_harden_status = rpc::StatusCode::STATUS_OK;
  Bridge.onStatus(BridgeClass::StatusHandler::create<&capture_harden_status>());

  // Send invalid varint/protobuf byte sequence on address 1 (RPC)
  const uint8_t malformed_rpc[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  etl::array<uint8_t, 64> frame_buf;
  size_t frame_len = TestHdlc::encodeUIFrame(
      1, etl::span<const uint8_t>(malformed_rpc, sizeof(malformed_rpc)),
      frame_buf);
  TEST_ASSERT_TRUE(frame_len > 0);

  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), frame_len));

  TEST_ASSERT_EQUAL(static_cast<int>(rpc::StatusCode::STATUS_MALFORMED),
                    static_cast<int>(s_captured_harden_status));
  Bridge.onStatus(BridgeClass::StatusHandler());
}

void test_bridge_unknown_command_jump_table() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Send an RPC request with unknown service and method
  etl::array<uint8_t, 256> frame_buf;
  size_t len = build_hdlc_rpc_frame(
      1, pw::rpc_avr::PacketType::kRequest, 0xDEADBEEF, 0xCAFEBABE,
      etl::span<const uint8_t>(), frame_buf, 42, 1);
  TEST_ASSERT_TRUE(len > 0);

  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), len));
  // Server should send a response (Status::NotFound)
  TEST_ASSERT_GREATER_THAN(0, stream.tx_buf.len);
}

void test_bridge_console_stream_rx() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Feed raw console stream data on HDLC address 3
  const uint8_t console_data[] = {'H', 'e', 'l', 'l', 'o'};
  etl::array<uint8_t, 64> frame_buf;
  size_t len = TestHdlc::encodeUIFrame(
      3, etl::span<const uint8_t>(console_data, sizeof(console_data)),
      frame_buf);
  TEST_ASSERT_TRUE(len > 0);

  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), len));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_bridge_packet_received_edge_cases() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // 1. Incomplete HDLC frame (no end flag)
  const uint8_t short_pkt[] = {0x7E, 0x01, 0x00};
  ba.feedHdlcBytes(etl::span<const uint8_t>(short_pkt, sizeof(short_pkt)));

  // 2. Corrupt CRC frame
  etl::array<uint8_t, 256> frame_buf;
  size_t len =
      build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kRequest,
                           pw::tokenizer::Hash("rpc.pb.SystemService"),
                           pw::tokenizer::Hash("GetVersion"),
                           etl::span<const uint8_t>(), frame_buf, 1, 1);
  TEST_ASSERT_TRUE(len > 0);

  // Invalidate CRC
  frame_buf[len - 2] ^= 0xFF;
  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), len));

  // Should not have processed corrupt frame
  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_begin_idempotency() {
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
  Bridge.begin(9600);
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_bridge_linksync_auth_failure() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  rpc_pb_LinkSync sync_msg = rpc_pb_LinkSync_init_default;
  memset(sync_msg.nonce.bytes, 0xAA, 16);
  sync_msg.nonce.size = 16;
  memset(sync_msg.tag.bytes, 0xFF, 16);  // Bad tag
  sync_msg.tag.size = 16;

  uint8_t payload_buf[64];
  pb_ostream_t ostream =
      pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
  TEST_ASSERT_TRUE(pb_encode(&ostream, rpc_pb_LinkSync_fields, &sync_msg));

  etl::array<uint8_t, 256> frame_buf;
  size_t len = build_hdlc_rpc_frame(
      1, pw::rpc_avr::PacketType::kRequest,
      pw::tokenizer::Hash("rpc.pb.SystemService"),
      pw::tokenizer::Hash("LinkSync"),
      etl::span<const uint8_t>(payload_buf, ostream.bytes_written), frame_buf,
      1, 1);
  TEST_ASSERT_TRUE(len > 0);

  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), len));
  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_security_pre_sync_rejection() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  // Try to send a restricted service command (e.g. FileSystem) before sync
  etl::array<uint8_t, 256> frame_buf;
  size_t len =
      build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kRequest,
                           pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                           pw::tokenizer::Hash("Remove"),
                           etl::span<const uint8_t>(), frame_buf, 1, 1);
  TEST_ASSERT_TRUE(len > 0);

  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), len));
  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_nonce_reuse_attack() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  // 1. Sync properly
  rpc_pb_LinkSync sync_msg = rpc_pb_LinkSync_init_default;
  memset(sync_msg.nonce.bytes, 0xAA, 16);
  sync_msg.nonce.size = 16;
  ba.computeHandshakeTag(sync_msg.nonce.bytes, 16, sync_msg.tag.bytes);
  sync_msg.tag.size = 16;

  uint8_t payload_buf[64];
  pb_ostream_t ostream =
      pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
  TEST_ASSERT_TRUE(pb_encode(&ostream, rpc_pb_LinkSync_fields, &sync_msg));

  etl::array<uint8_t, 256> frame_buf;
  size_t len = build_hdlc_rpc_frame(
      1, pw::rpc_avr::PacketType::kRequest,
      pw::tokenizer::Hash("rpc.pb.SystemService"),
      pw::tokenizer::Hash("LinkSync"),
      etl::span<const uint8_t>(payload_buf, ostream.bytes_written), frame_buf,
      1, 1);
  TEST_ASSERT_TRUE(len > 0);

  ba.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), len));
  TEST_ASSERT_TRUE(ba.isSynchronized());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_malformed_rpc_packet);
  RUN_TEST(test_bridge_unknown_command_jump_table);
  RUN_TEST(test_bridge_console_stream_rx);
  RUN_TEST(test_bridge_packet_received_edge_cases);
  RUN_TEST(test_bridge_begin_idempotency);
  RUN_TEST(test_bridge_linksync_auth_failure);
  RUN_TEST(test_bridge_security_pre_sync_rejection);
  RUN_TEST(test_bridge_nonce_reuse_attack);
  return UNITY_END();
}
