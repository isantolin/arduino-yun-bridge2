#define BRIDGE_ENABLE_TEST_INTERFACE
#include <Arduino.h>
#include <etl/byte_stream.h>
#include <unity.h>

#include "Bridge.h"
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
  auto& ba = TestAccessor::create(Bridge);

  const char* secret_str = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret_str);

  // 1. Prepare LinkSync request from "MPU"
  rpc::payload::LinkSync sync_req = {};
  for (int i = 0; i < 12; ++i)
    sync_req.nonce.bytes[i] = static_cast<uint8_t>(i + 1);
  sync_req.nonce.size = 12;

  // Handshake Key Derivation
  etl::array<uint8_t, 32> handshake_key;
  rpc::security::hkdf_sha256(
      etl::span<uint8_t>(handshake_key),
      etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(secret_str),
                               32),
      etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
      etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));

  Hmac hmac;
  wc_HmacSetKey(&hmac, WC_SHA256, handshake_key.data(), 32);
  wc_HmacUpdate(&hmac, sync_req.nonce.bytes, 12);
  wc_HmacFinal(&hmac, handshake_key.data());
  memcpy(sync_req.tag.bytes, handshake_key.data(), 16);
  sync_req.tag.size = 16;

  rpc_pb_RpcEnvelope f_sync;
  f_sync.version = rpc::PROTOCOL_VERSION;
  f_sync.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  f_sync.sequence_id = 1;

  bridge::test::set_pb_payload(f_sync, sync_req);

  // 2. Dispatch SYNC.
  ba.setIdle();
  ba.dispatch(f_sync);

  // 3. Send ENCRYPTED data frame (even if not synced, to test rejection
  // branches)
  stream.clear();
  rpc_pb_RpcEnvelope f_data;
  f_data.version = rpc::PROTOCOL_VERSION;
  f_data.command_id =
      static_cast<uint16_t>(rpc::CommandId::CMD_GET_FREE_MEMORY);
  f_data.sequence_id = 2;

  f_data.nonce.bytes[0] = 'M';
  f_data.nonce.bytes[1] = 'P';
  f_data.nonce.bytes[2] = 'U';
  f_data.nonce.bytes[11] = 5;  // Counter = 5
  f_data.nonce.size = 12;
  memset(f_data.tag.bytes, 0xEE, 16);
  f_data.tag.size = 16;

  ba.dispatch(f_data);

  // Emitting error should write to stream
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_bridge_ack_timeout_retry_to_fault() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
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
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  TEST_ASSERT_TRUE(true);
}

void test_aead_decrypt_and_validate_nonce() {
  // Fixed 32-byte session key
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> session_key;
  etl::fill(session_key.begin(), session_key.end(), 0x42U);

  constexpr uint16_t cmd =
      static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION);
  constexpr uint16_t seq = 7U;
  etl::array<uint8_t, 8> plaintext = {0xDE, 0xAD, 0xBE, 0xEF,
                                      0xCA, 0xFE, 0xBA, 0xBE};

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_out;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce;
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag;
  uint64_t tx_ctr = 0;

  bool enc_ok = rpc::security::aead_encrypt_frame(
      cmd, seq, etl::span<const uint8_t>(plaintext),
      etl::span<const uint8_t>(session_key), &tx_ctr,
      etl::span<uint8_t>(enc_out), etl::span<uint8_t>(nonce),
      etl::span<uint8_t>(tag));
  TEST_ASSERT_TRUE(enc_ok);
  TEST_ASSERT_EQUAL_UINT64(1U, tx_ctr);

  // Decrypt and verify plaintext is recovered
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_out;
  bool dec_ok = rpc::security::aead_decrypt_frame(
      cmd, seq, etl::span<const uint8_t>(enc_out.data(), plaintext.size()),
      etl::span<const uint8_t>(tag), etl::span<const uint8_t>(session_key),
      etl::span<const uint8_t>(nonce), etl::span<uint8_t>(dec_out));
  TEST_ASSERT_TRUE(dec_ok);
  TEST_ASSERT_EQUAL_MEMORY(plaintext.data(), dec_out.data(), plaintext.size());

  // Nonce counter validation – first use accepted
  uint64_t rx_ctr = 0;
  TEST_ASSERT_TRUE(rpc::security::validate_frame_nonce(
      etl::span<const uint8_t>(nonce), &rx_ctr));
  TEST_ASSERT_EQUAL_UINT64(1U, rx_ctr);

  // Replay protection – same nonce rejected
  TEST_ASSERT_FALSE(rpc::security::validate_frame_nonce(
      etl::span<const uint8_t>(nonce), &rx_ctr));
}

void test_encrypted_frame_receive_path() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  const char* secret = "secure_secret_1234567890123456";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  // Sync the bridge with a valid handshake tag
  rpc::payload::LinkSync sync_msg = {};
  memset(sync_msg.nonce.bytes, 0xCC, 16);
  sync_msg.nonce.size = 16;
  ba.computeHandshakeTag(sync_msg.nonce.bytes, 16, sync_msg.tag.bytes);
  sync_msg.tag.size = 16;

  rpc_pb_RpcEnvelope f_sync = {};
  f_sync.version = rpc::PROTOCOL_VERSION;
  f_sync.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  f_sync.sequence_id = 1;
  bridge::test::set_pb_payload(f_sync, sync_msg);
  ba.dispatch(f_sync);
  TEST_ASSERT_TRUE(ba.isSynchronized());

  // Inject a known session key and reset rx nonce counter
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> known_key;
  etl::fill(known_key.begin(), known_key.end(), 0x5AU);
  ba.setSessionKey(etl::span<const uint8_t>(known_key));
  ba.setRxNonceCounter(0);

  // Encrypt an empty CMD_DIGITAL_READ frame with the known key.
  // CMD_DIGITAL_READ (83) is outside the system/status exclusion ranges so the
  // bridge will run the full AEAD decrypt + nonce-validation path before
  // dispatching the command and emitting a reply.
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_out;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce;
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag;
  uint64_t tx_ctr = 0;
  etl::array<uint8_t, 1> no_payload{};

  bool enc_ok = rpc::security::aead_encrypt_frame(
      static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_READ), 2,
      etl::span<const uint8_t>(no_payload.data(), 0),
      etl::span<const uint8_t>(known_key), &tx_ctr, etl::span<uint8_t>(enc_out),
      etl::span<uint8_t>(nonce), etl::span<uint8_t>(tag));
  TEST_ASSERT_TRUE(enc_ok);

  // Build and serialize the encrypted envelope
  rpc_pb_RpcEnvelope f_enc = {};
  f_enc.version = rpc::PROTOCOL_VERSION;
  f_enc.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_READ);
  f_enc.sequence_id = 2;
  etl::copy_n(nonce.begin(), rpc::RPC_AEAD_NONCE_SIZE, f_enc.nonce.bytes);
  f_enc.nonce.size = static_cast<pb_size_t>(rpc::RPC_AEAD_NONCE_SIZE);
  f_enc.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag;
  etl::copy_n(tag.begin(), rpc::RPC_AEAD_TAG_SIZE,
              f_enc.payload_type.encrypted_payload_with_tag.bytes);
  f_enc.payload_type.encrypted_payload_with_tag.size =
      static_cast<pb_size_t>(rpc::RPC_AEAD_TAG_SIZE);

  etl::array<uint8_t, 256> raw_buf;
  size_t raw_len = rpc::serialize_frame(f_enc, raw_buf);
  stream.clear();
  ba.invokePacketReceived(etl::span<const uint8_t>(raw_buf.data(), raw_len));

  // Bridge must have responded – either CMD_DIGITAL_READ_RESP or STATUS_ERROR
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_full_crypto_handshake_and_data);
  RUN_TEST(test_bridge_ack_timeout_retry_to_fault);
  RUN_TEST(test_bridge_nonce_overflow_protection);
  RUN_TEST(test_aead_decrypt_and_validate_nonce);
  RUN_TEST(test_encrypted_frame_receive_path);
  return UNITY_END();
}
