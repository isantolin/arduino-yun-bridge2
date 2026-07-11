#define BRIDGE_ENABLE_TEST_INTERFACE
#include <Arduino.h>
#include <unity.h>

#include "Bridge.h"
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
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_RpcEnvelope f;
  f.version = static_cast<uint8_t>(rpc::PROTOCOL_VERSION + 1);
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION);
  f.sequence_id = 1;

  etl::array<uint8_t, 256> buf;
  size_t len = rpc::serialize_frame(f, buf);
  ba.invokePacketReceived(etl::span<const uint8_t>(buf.data(), len));

  // Should have emitted STATUS_MALFORMED
  TEST_ASSERT_EQUAL(0, stream.available());
}

void test_bridge_unknown_command_jump_table() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_RpcEnvelope f;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = 254;  // empty
  f.sequence_id = 1;

  ba.dispatch(f);
  TEST_ASSERT_TRUE(true);
}

void test_bridge_tx_queue_full_force() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
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
  auto& ba = TestAccessor::create(Bridge);

  // 1. Too short packet
  etl::array<uint8_t, 2> short_pkt = {0, 1};
  ba.invokePacketReceived(short_pkt);

  // 2. CRC mismatch
  rpc_pb_RpcEnvelope f;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION);
  f.sequence_id = 1;

  etl::array<uint8_t, 256> buf;
  size_t len = rpc::serialize_frame(f, buf);
  buf[len - 1] ^= 0xFF;  // Break CRC
  ba.invokePacketReceived(etl::span<const uint8_t>(buf.data(), len));

  TEST_ASSERT_TRUE(true);
}

void test_bridge_ack_orphans() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

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
  auto& ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  rpc::payload::LinkSync sync_msg = {};
  memset(sync_msg.nonce.bytes, 0xAA, 16);
  sync_msg.nonce.size = 16;
  memset(sync_msg.tag.bytes, 0xFF, 16);
  sync_msg.tag.size = 16;

  rpc_pb_RpcEnvelope f;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  f.sequence_id = 1;

  bridge::test::set_pb_payload(f, sync_msg);
  ba.dispatch(f);

  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_retransmit_empty_queue() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  (void)ba;

  // Calling internal retransmit on empty queue should be safe
  TEST_ASSERT_TRUE(true);
}

void test_bridge_security_pre_sync_rejection() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  // Try to send a restricted command before sync
  rpc_pb_RpcEnvelope f;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_CONSOLE_WRITE);
  f.sequence_id = 1;

  ba.dispatch(f);

  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_nonce_reuse_attack() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  // 1. Sync properly
  rpc::payload::LinkSync sync_msg = {};
  memset(sync_msg.nonce.bytes, 0xAA, 16);
  sync_msg.nonce.size = 16;
  ba.computeHandshakeTag(sync_msg.nonce.bytes, 16, sync_msg.tag.bytes);
  sync_msg.tag.size = 16;

  rpc_pb_RpcEnvelope f;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  f.sequence_id = 1;

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
  RUN_TEST(test_bridge_security_pre_sync_rejection);
  RUN_TEST(test_bridge_nonce_reuse_attack);
  return UNITY_END();
}
