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
constexpr etl::array<uint8_t, 10U> kVectorSecret = {
    {'s', 'h', 'a', 'r', 'e', 'd', '-', 'k', 'e', 'y'}};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> kVectorNonce = {
    0xABU, 0xABU, 0xABU, 0xABU, 0xABU, 0xABU, 0xABU, 0xABU,
    0xABU, 0xABU, 0xABU, 0xABU, 0xABU, 0xABU, 0xABU, 0xABU};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> kExpectedTag = {
    0xF0U, 0x7AU, 0x5AU, 0xA1U, 0x34U, 0x05U, 0x71U, 0x5BU,
    0x8B,  0x54U, 0xEFU, 0xDEU, 0x57U, 0xACU, 0x81U, 0x30U};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH>
    kExpectedResponseTag = {0x46U, 0x82U, 0x99U, 0xFAU, 0xC4U, 0xFBU,
                            0x82U, 0xDEU, 0x13U, 0x91U, 0xF8U, 0xA9U,
                            0xA8U, 0x4BU, 0xAFU, 0xD5U};

}  // namespace

void setUp(void) {}
void tearDown(void) {}

void test_handshake_tag_generation_rejects_invalid_buffers(void) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> output_tag = {0xAAU};
  const bool empty_secret_result = rpc::security::handshake_generate_tag(
      etl::span<const uint8_t>(), kHandshakeNonce, output_tag);
  TEST_ASSERT_FALSE(empty_secret_result);
  TEST_ASSERT_EQUAL_UINT8(0U, output_tag[0]);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH - 1U> short_output = {
      0xAAU};
  const bool short_output_result = rpc::security::handshake_generate_tag(
      kSecret, kHandshakeNonce, short_output);
  TEST_ASSERT_FALSE(short_output_result);
  TEST_ASSERT_EQUAL_UINT8(0U, short_output[0]);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> output_for_short_nonce = {
      0xAAU};
  const bool short_nonce_result = rpc::security::handshake_generate_tag(
      kSecret,
      etl::span<const uint8_t>(kHandshakeNonce.data(),
                               kHandshakeNonce.size() - 1U),
      output_for_short_nonce);
  TEST_ASSERT_FALSE(short_nonce_result);
  TEST_ASSERT_EQUAL_UINT8(0U, output_for_short_nonce[0]);
}

void test_handshake_tag_generation_and_verification(void) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> generated_tag = {};
  const bool generated =
      rpc::security::handshake_generate_tag(kSecret, kHandshakeNonce,
                                            generated_tag);
  TEST_ASSERT_TRUE(generated);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> received_tag = {};
  etl::copy_n(generated_tag.begin(), received_tag.size(), received_tag.begin());
  TEST_ASSERT_TRUE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce, received_tag));

  received_tag[0] ^= 0xFFU;
  TEST_ASSERT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce, received_tag));

  TEST_ASSERT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce, etl::span<const uint8_t>()));

  TEST_ASSERT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce,
      etl::span<const uint8_t>(received_tag.data(), received_tag.size() - 1U)));
}

void test_handshake_tag_known_answer(void) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> actual_tag = {};
  TEST_ASSERT_TRUE(rpc::security::handshake_generate_tag(
      kVectorSecret, kVectorNonce, actual_tag));
  TEST_ASSERT_EQUAL_UINT8_ARRAY(kExpectedTag.data(), actual_tag.data(),
                                kExpectedTag.size());
  TEST_ASSERT_TRUE(rpc::security::handshake_verify_tag(
      kVectorSecret, kVectorNonce, kExpectedTag));

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> response_tag = {};
  TEST_ASSERT_TRUE(rpc::security::handshake_generate_response_tag(
      kVectorSecret, kVectorNonce, response_tag));
  TEST_ASSERT_EQUAL_UINT8_ARRAY(kExpectedResponseTag.data(),
                                response_tag.data(), response_tag.size());
  TEST_ASSERT_FALSE(etl::equal(actual_tag.begin(), actual_tag.end(),
                               response_tag.begin()));
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
  RUN_TEST(test_handshake_tag_generation_rejects_invalid_buffers);
  RUN_TEST(test_handshake_tag_generation_and_verification);
  RUN_TEST(test_handshake_tag_known_answer);
#if BRIDGE_ENABLE_POST_TESTS
  RUN_TEST(test_cryptographic_self_tests_run);
#endif
  return UNITY_END();
}
