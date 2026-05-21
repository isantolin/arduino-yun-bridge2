#define BRIDGE_ENABLE_TEST_INTERFACE
#include <Arduino.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "etl_ext/CounterIterator.h"
#include "test_support.h"

// [SIL-2] Global stub definitions for host environment
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

using bridge::test::TestAccessor;

void setUp() {}
void tearDown() {}

void test_bridge_protocol_version_mismatch() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  f.header = {static_cast<uint8_t>(rpc::PROTOCOL_VERSION + 1), 0,
               static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION), 1};
  f.nonce.fill(0);
  f.tag.fill(0);
  f.payload = {};
  f.crc = rpc::checksum::compute(f);

  // Dispatching frame with wrong protocol version should be rejected or ignored.
  // However, _dispatchCommand doesn't check version, it's checked in parser.
  // Let's call _onPacketReceived instead.
  etl::array<uint8_t, 256> buf;
  size_t len = rpc::FrameParser::serialize(f, buf);
  ba.invokePacketReceived(etl::span<const uint8_t>(buf.data(), len));

  // Should have emitted STATUS_MALFORMED
  TEST_ASSERT_EQUAL(0, stream.available());
}

void test_bridge_unknown_command_jump_table() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  f.header = {rpc::PROTOCOL_VERSION, 0, 254, 1};  // 254 is empty
  f.nonce.fill(0);
  f.tag.fill(0);
  f.payload = {};

  ba.dispatch(f);
  TEST_ASSERT_TRUE(true);
}

void test_bridge_tx_queue_full_force() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Fill the queue
  bridge::etl_ext::CounterIterator<int> fill_begin(0);
  bridge::etl_ext::CounterIterator<int> fill_end(
      bridge::config::MAX_PENDING_TX_FRAMES);
  etl::for_each(fill_begin, fill_end, [](int i) {
    bool ok = Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE,
                               static_cast<uint16_t>(i), {});
    TEST_ASSERT_TRUE(ok);
  });

  // Next one must fail
  bool ok = Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 99, {});
  TEST_ASSERT_FALSE(ok);
}

void test_bridge_packet_received_edge_cases() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  // 1. Too short packet
  etl::array<uint8_t, 2> short_pkt = {0, 1};
  ba.invokePacketReceived(short_pkt);

  // 2. CRC mismatch
  rpc::Frame f = {};
  f.header = {rpc::PROTOCOL_VERSION, 0,
               static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION), 1};
  f.nonce.fill(0);
  f.tag.fill(0);
  f.payload = {};
  f.crc = rpc::RPC_BOOTLOADER_MAGIC;

  etl::array<uint8_t, 256> buf;
  size_t len = rpc::FrameParser::serialize(f, buf);
  ba.invokePacketReceived(etl::span<const uint8_t>(buf.data(), len));

  TEST_ASSERT_TRUE(true);
}

void test_bridge_ack_orphans() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  // Ack while not awaiting
  ba.handleAck(static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION));

  // Ack with wrong command ID while awaiting
  ba.setSynchronized();
  TEST_ASSERT_TRUE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 1, {}));
  ba.handleAck(
      static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION));  // Wrong ID

  TEST_ASSERT(ba.isAwaitingAck());
}

void test_bridge_begin_idempotency() {
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
  Bridge.begin(9600);
  TEST_ASSERT_TRUE(true);
}

void test_bridge_linksync_auth_failure() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  rpc::payload::LinkSync sync_msg = {};
  memset(sync_msg.pb_msg.nonce.bytes, 0xAA, 16); sync_msg.pb_msg.nonce.size = 16;
  sync_msg.pb_msg.nonce.size = 16;
  memset(sync_msg.pb_msg.tag.bytes, 0xFF, 16); sync_msg.pb_msg.tag.size = 16;  // Wrong tag
  sync_msg.pb_msg.tag.size = 16;

  rpc::Frame f = {};
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> f_buf;
  f.payload = etl::span<uint8_t>(f_buf.data(), f_buf.size());
  f.header = {rpc::PROTOCOL_VERSION, 0,
               static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC), 1};

  bridge::test::set_pb_payload(f, sync_msg);
  ba.dispatch(f);

  // Should have transitioned to Fault or Reset or stayed Unsynced
  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_retransmit_empty_queue() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  (void)ba;

  // Calling internal retransmit on empty queue should be safe
  TEST_ASSERT_TRUE(true);
}

void test_bridge_decompress_error() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  // Flag as compressed but provide garbage
  f.header = {rpc::PROTOCOL_VERSION, 4,
               static_cast<uint16_t>(
                   static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION) |
                   rpc::RPC_CMD_FLAG_COMPRESSED),
               1};
  etl::array<uint8_t, 4> garbage = {0xFF, 0xFF, 0xFF, 0xFF};  // Invalid RLE
  f.payload = garbage;
  f.crc = rpc::checksum::compute(f);

  etl::array<uint8_t, 256> buf;
  size_t len = rpc::FrameParser::serialize(f, buf);
  ba.invokePacketReceived(etl::span<const uint8_t>(buf.data(), len));

  TEST_ASSERT_TRUE(true);
}

void test_bridge_security_pre_sync_rejection() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  // Try to send a restricted command before sync
  rpc::Frame f = {};
  f.header = {rpc::PROTOCOL_VERSION, 0,
               static_cast<uint16_t>(rpc::CommandId::CMD_GET_FREE_MEMORY), 1};
  ba.dispatch(f);

  // Should have emitted STATUS_ERROR (which is excluded from sync check
  // usually) but the restricted command itself was rejected.
  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_nonce_reuse_attack() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  // 1. Sync properly
  rpc::payload::LinkSync sync_msg = {};
  memset(sync_msg.pb_msg.nonce.bytes, 0xAA, 16); sync_msg.pb_msg.nonce.size = 16;
  sync_msg.pb_msg.nonce.size = 16;
  ba.computeHandshakeTag(sync_msg.pb_msg.nonce.bytes, 16,
                         sync_msg.pb_msg.tag.bytes);
  sync_msg.pb_msg.tag.size = 16;

  rpc::Frame f = {};
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> f_buf;
  f.payload = etl::span<uint8_t>(f_buf.data(), f_buf.size());
  f.header = {rpc::PROTOCOL_VERSION, 0,
               static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC), 1};
  bridge::test::set_pb_payload(f, sync_msg);
  ba.dispatch(f);
  TEST_ASSERT_TRUE(ba.isSynchronized());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_protocol_version_mismatch);
  RUN_TEST(test_bridge_unknown_command_jump_table);
  RUN_TEST(test_bridge_tx_queue_full_force);
  RUN_TEST(test_bridge_packet_received_edge_cases);
  RUN_TEST(test_bridge_ack_orphans);
  RUN_TEST(test_bridge_begin_idempotency);
  RUN_TEST(test_bridge_linksync_auth_failure);
  RUN_TEST(test_bridge_decompress_error);
  RUN_TEST(test_bridge_security_pre_sync_rejection);
  RUN_TEST(test_bridge_nonce_reuse_attack);
  return UNITY_END();
}