/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/byte_stream.h>
#include <etl/span.h>
#include <unity.h>

#include "../src/config/bridge_config.h"
#include "../src/protocol/mcu_wire.pb.h"
#include "../src/security/security.h"

namespace {

constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> kHandshakeNonce =
    {1U, 2U, 3U, 4U, 5U, 6U, 7U, 8U, 9U, 10U, 11U, 12U, 13U, 14U, 15U, 16U};
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
#if BRIDGE_ENABLE_POST_TESTS
  RUN_TEST(test_cryptographic_self_tests_run);
#endif
  return UNITY_END();
}
