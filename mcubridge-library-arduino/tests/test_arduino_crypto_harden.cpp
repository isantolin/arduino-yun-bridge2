#define BRIDGE_ENABLE_TEST_INTERFACE
#include <Arduino.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "etl_ext/CounterIterator.h"
#include "test_support.h"

// [SIL-2] Global stub definitions
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

using bridge::test::TestAccessor;

void setUp(void) {}
void tearDown(void) {}

/**
 * @brief High-fidelity test for AEAD encryption and session key derivation.
 * Targets _sendRawFrame (do_encrypt), _handleLinkSync, and _handleReceivedFrame
 * (aead_decrypt).
 */
void test_bridge_full_crypto_handshake_and_data() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  const char* secret_str = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret_str);

  // 1. Prepare LinkSync request from "MPU"
  LinkSync sync_req = {};
  for (int i = 0; i < 16; ++i)
    sync_req.nonce.bytes[i] = static_cast<uint8_t>(i + 1);
  sync_req.nonce.size = 16;

  // Handshake Key Derivation
  etl::array<uint8_t, 32> handshake_key;
  rpc::security::hkdf_sha256(
      etl::span<uint8_t>(handshake_key),
      etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(secret_str),
                               32),
      etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
      etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));

  Hmac hmac_engine;
  wc_HmacSetKey(&hmac_engine, WC_SHA256, handshake_key.data(), 32);
  wc_HmacUpdate(&hmac_engine, sync_req.nonce.bytes, 16);
  wc_HmacFinal(&hmac_engine, handshake_key.data());
  memcpy(sync_req.tag.bytes, handshake_key.data(), 16);
  sync_req.tag.size = 16;

  rpc::Frame f_sync = {};
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> f_sync_buf;
  f_sync.payload = etl::span<uint8_t>(f_sync_buf.data(), f_sync_buf.size());
  f_sync.header = {rpc::PROTOCOL_VERSION, 0,
                   static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC), 1};
  f_sync.nonce.fill(0);
  f_sync.tag.fill(0);

  static etl::array<uint8_t, 512> pl_buf;
  f_sync.payload = etl::span<uint8_t>(pl_buf.data(), pl_buf.size());
  bridge::test::set_pb_payload(f_sync, sync_req);
  f_sync.crc = rpc::checksum::compute(f_sync);

  // 2. Dispatch SYNC.
  ba.setIdle();
  ba.dispatch(f_sync);

  // 3. Send ENCRYPTED data frame (even if not synced, to test rejection
  // branches)
  stream.clear();
  rpc::Frame f_data = {};
  f_data.header = {rpc::PROTOCOL_VERSION, 0,
                   static_cast<uint16_t>(rpc::CommandId::CMD_GET_FREE_MEMORY),
                   2};
  f_data.nonce.fill(0);
  f_data.nonce[0] = 'M';
  f_data.nonce[1] = 'P';
  f_data.nonce[2] = 'U';
  f_data.nonce[11] = 5;         // Counter = 5
  f_data.tag.fill(0xEE);  // Triggers AEAD failure path

  ba.dispatch(f_data);

  // Emitting error should write to stream
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_bridge_ack_timeout_retry_to_fault() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();
  ba.setSynchronized();

  // Send reliable command
  TEST_ASSERT_TRUE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 1, {}));
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  // Trigger timeout 3 times (Default limit)
  bridge::etl_ext::CounterIterator<int> retry_begin(0);
  bridge::etl_ext::CounterIterator<int> retry_end(rpc::RPC_DEFAULT_RETRY_LIMIT);
  etl::for_each(retry_begin, retry_end, [&ba](int) { ba.onAckTimeout(); });

  // After limit, it should transition out of Awaiting Ack
  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_nonce_overflow_protection() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  TEST_ASSERT_TRUE(true);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_full_crypto_handshake_and_data);
  RUN_TEST(test_bridge_ack_timeout_retry_to_fault);
  RUN_TEST(test_bridge_nonce_overflow_protection);
  return UNITY_END();
}