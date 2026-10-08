/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include <unity.h>

#include "../src/config/bridge_config.h"
#include "../src/protocol/rpc_structs.h"
#include "../src/security/security.h"

namespace {

constexpr uint16_t kCommandId =
    rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
constexpr uint16_t kSequenceId = 1U;
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> kHandshakeNonce =
    {1U, 2U, 3U, 4U, 5U, 6U, 7U, 8U, 9U, 10U, 11U, 12U, 13U, 14U, 15U, 16U};
constexpr etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> kAeadKey = {1U};
constexpr etl::array<uint8_t, 8U> kPayload = {
    {'T', 'E', 'S', 'T', 'D', 'A', 'T', 'A'}};
constexpr etl::array<uint8_t, 13U> kSecret = {
    {'s', 'h', 'a', 'r', 'e', 'd', '-', 's', 'e', 'c', 'r', 'e', 't'}};

}  // namespace

void setUp(void) {}
void tearDown(void) {}

void test_handshake_authenticate_rejects_invalid_buffers(void) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> output_tag = {0xAAU};
  const bool empty_secret_result = rpc::security::handshake_authenticate(
      etl::span<const uint8_t>(), kHandshakeNonce, etl::span<const uint8_t>(),
      output_tag);
  TEST_ASSERT_FALSE(empty_secret_result);
  TEST_ASSERT_EQUAL_UINT8(0U, output_tag[0]);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH - 1U> short_output = {
      0xAAU};
  const bool short_output_result = rpc::security::handshake_authenticate(
      kSecret, kHandshakeNonce, etl::span<const uint8_t>(), short_output);
  TEST_ASSERT_FALSE(short_output_result);
  TEST_ASSERT_EQUAL_UINT8(0U, short_output[0]);
}

void test_handshake_authenticate_truncates_and_checks_tag(void) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> full_tag = {};
  const bool generated = rpc::security::handshake_authenticate(
      kSecret, kHandshakeNonce, etl::span<const uint8_t>(), full_tag);
  TEST_ASSERT_TRUE(generated);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> received_tag = {};
  etl::copy_n(full_tag.begin(), received_tag.size(), received_tag.begin());
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> output_tag = {};
  const bool matching_tag = rpc::security::handshake_authenticate(
      kSecret, kHandshakeNonce, received_tag, output_tag);
  TEST_ASSERT_TRUE(matching_tag);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(received_tag.data(), output_tag.data(),
                                received_tag.size());

  received_tag[0] ^= 0xFFU;
  const bool mismatching_tag = rpc::security::handshake_authenticate(
      kSecret, kHandshakeNonce, received_tag, output_tag);
  TEST_ASSERT_FALSE(mismatching_tag);
}

void test_derive_session_key_clears_invalid_outputs(void) {
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> empty_secret_output = {0xA5U};
  rpc::security::derive_session_key(etl::span<const uint8_t>(), kHandshakeNonce,
                                    empty_secret_output);
  const bool empty_secret_cleared =
      etl::all_of(empty_secret_output.begin(), empty_secret_output.end(),
                  [](uint8_t value) { return value == 0U; });
  TEST_ASSERT_TRUE(empty_secret_cleared);

  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE - 1U> short_output = {0xA5U};
  rpc::security::derive_session_key(kSecret, kHandshakeNonce, short_output);
  const bool short_output_cleared =
      etl::all_of(short_output.begin(), short_output.end(),
                  [](uint8_t value) { return value == 0U; });
  TEST_ASSERT_TRUE(short_output_cleared);

  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> valid_output = {};
  rpc::security::derive_session_key(kSecret, kHandshakeNonce, valid_output);
  const bool valid_key_derived =
      etl::any_of(valid_output.begin(), valid_output.end(),
                  [](uint8_t value) { return value != 0U; });
  TEST_ASSERT_TRUE(valid_key_derived);
}

void test_aead_encrypt_rejects_invalid_buffer_sizes(void) {
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE - 1U> short_key = {};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, kPayload.size()> output = {};
  uint64_t nonce_counter = 0U;

  const bool invalid_key = rpc::security::aead_encrypt_frame(
      kCommandId, kSequenceId, kPayload, short_key, &nonce_counter, output,
      nonce, tag);
  TEST_ASSERT_FALSE(invalid_key);

  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE - 1U> short_nonce = {};
  const bool invalid_nonce = rpc::security::aead_encrypt_frame(
      kCommandId, kSequenceId, kPayload, kAeadKey, &nonce_counter, output,
      short_nonce, tag);
  TEST_ASSERT_FALSE(invalid_nonce);

  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE - 1U> short_tag = {};
  const bool invalid_tag = rpc::security::aead_encrypt_frame(
      kCommandId, kSequenceId, kPayload, kAeadKey, &nonce_counter, output,
      nonce, short_tag);
  TEST_ASSERT_FALSE(invalid_tag);

  etl::array<uint8_t, kPayload.size() - 1U> short_output = {};
  const bool insufficient_output = rpc::security::aead_encrypt_frame(
      kCommandId, kSequenceId, kPayload, kAeadKey, &nonce_counter, short_output,
      nonce, tag);
  TEST_ASSERT_FALSE(insufficient_output);
  TEST_ASSERT_EQUAL_UINT64(0U, nonce_counter);
}

void test_aead_decrypt_validates_buffers_and_tag(void) {
  etl::array<uint8_t, kPayload.size()> ciphertext = {};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag = {};
  uint64_t nonce_counter = 0U;
  const bool encrypted = rpc::security::aead_encrypt_frame(
      kCommandId, kSequenceId, kPayload, kAeadKey, &nonce_counter, ciphertext,
      nonce, tag);
  TEST_ASSERT_TRUE(encrypted);

  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE - 1U> short_key = {};
  etl::array<uint8_t, kPayload.size()> plaintext = {};
  const bool invalid_key = rpc::security::aead_decrypt_frame(
      kCommandId, kSequenceId, ciphertext, short_key, nonce, tag, plaintext);
  TEST_ASSERT_FALSE(invalid_key);

  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE - 1U> short_nonce = {};
  const bool invalid_nonce =
      rpc::security::aead_decrypt_frame(kCommandId, kSequenceId, ciphertext,
                                        kAeadKey, short_nonce, tag, plaintext);
  TEST_ASSERT_FALSE(invalid_nonce);

  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE - 1U> short_tag = {};
  const bool invalid_tag_size =
      rpc::security::aead_decrypt_frame(kCommandId, kSequenceId, ciphertext,
                                        kAeadKey, nonce, short_tag, plaintext);
  TEST_ASSERT_FALSE(invalid_tag_size);

  etl::array<uint8_t, kPayload.size() - 1U> short_output = {};
  const bool insufficient_output = rpc::security::aead_decrypt_frame(
      kCommandId, kSequenceId, ciphertext, kAeadKey, nonce, tag, short_output);
  TEST_ASSERT_FALSE(insufficient_output);

  const bool decrypted = rpc::security::aead_decrypt_frame(
      kCommandId, kSequenceId, ciphertext, kAeadKey, nonce, tag, plaintext);
  TEST_ASSERT_TRUE(decrypted);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(kPayload.data(), plaintext.data(),
                                kPayload.size());

  tag[0] ^= 0xFFU;
  const bool invalid_tag_content = rpc::security::aead_decrypt_frame(
      kCommandId, kSequenceId, ciphertext, kAeadKey, nonce, tag, plaintext);
  TEST_ASSERT_FALSE(invalid_tag_content);
}

void test_nonce_counter_accepts_random_prefix_and_rejects_replay(void) {
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {0x12U, 0x34U, 0x56U,
                                                         0x78U};
  nonce[11] = 1U;
  uint64_t last_seen_counter = 0U;

  const bool first_nonce =
      rpc::security::validate_frame_nonce(nonce, &last_seen_counter);
  TEST_ASSERT_TRUE(first_nonce);
  TEST_ASSERT_EQUAL_UINT64(1U, last_seen_counter);

  const bool replayed_nonce =
      rpc::security::validate_frame_nonce(nonce, &last_seen_counter);
  TEST_ASSERT_FALSE(replayed_nonce);
  TEST_ASSERT_EQUAL_UINT64(1U, last_seen_counter);

  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> newer_nonce = nonce;
  newer_nonce[11] = 2U;
  const bool no_counter_tracking =
      rpc::security::validate_frame_nonce(newer_nonce, nullptr);
  TEST_ASSERT_TRUE(no_counter_tracking);

  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE - 1U> short_nonce = {};
  const bool short_nonce_rejected =
      rpc::security::validate_frame_nonce(short_nonce, nullptr);
  TEST_ASSERT_FALSE(short_nonce_rejected);

  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE + 1U> long_nonce = {};
  const bool long_nonce_rejected =
      rpc::security::validate_frame_nonce(long_nonce, nullptr);
  TEST_ASSERT_FALSE(long_nonce_rejected);
}

void test_bridge_nonce_overflow_protection(void) {
  uint64_t nonce_counter = rpc::RPC_NONCE_COUNTER_MASK - 1U;
  etl::array<uint8_t, kPayload.size()> output = {};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag = {};

  const bool last_valid_nonce = rpc::security::aead_encrypt_frame(
      kCommandId, kSequenceId, kPayload, kAeadKey, &nonce_counter, output,
      nonce, tag);
  TEST_ASSERT_TRUE(last_valid_nonce);
  TEST_ASSERT_EQUAL_UINT64(rpc::RPC_NONCE_COUNTER_MASK, nonce_counter);

  const bool overflow_rejected = rpc::security::aead_encrypt_frame(
      kCommandId, kSequenceId, kPayload, kAeadKey, &nonce_counter, output,
      nonce, tag);
  TEST_ASSERT_FALSE(overflow_rejected);
  TEST_ASSERT_EQUAL_UINT64(rpc::RPC_NONCE_COUNTER_MASK, nonce_counter);
}

#if BRIDGE_ENABLE_POST_TESTS
void test_cryptographic_self_tests_run(void) {
  TEST_ASSERT_TRUE(rpc::security::run_cryptographic_self_tests());
}
#endif

int main(int argc, char** argv) {
  static_cast<void>(argc);
  static_cast<void>(argv);
  UNITY_BEGIN();
  RUN_TEST(test_handshake_authenticate_rejects_invalid_buffers);
  RUN_TEST(test_handshake_authenticate_truncates_and_checks_tag);
  RUN_TEST(test_derive_session_key_clears_invalid_outputs);
  RUN_TEST(test_aead_encrypt_rejects_invalid_buffer_sizes);
  RUN_TEST(test_aead_decrypt_validates_buffers_and_tag);
  RUN_TEST(test_nonce_counter_accepts_random_prefix_and_rejects_replay);
  RUN_TEST(test_bridge_nonce_overflow_protection);
#if BRIDGE_ENABLE_POST_TESTS
  RUN_TEST(test_cryptographic_self_tests_run);
#endif
  return UNITY_END();
}
