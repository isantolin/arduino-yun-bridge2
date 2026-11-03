/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/byte_stream.h>
#include <etl/span.h>
extern "C" {
#include <mbedtls/constant_time.h>
}
#include <psa/crypto.h>
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
  const bool generated = rpc::security::handshake_generate_tag(
      kSecret, kHandshakeNonce, generated_tag);
  TEST_ASSERT_TRUE(generated);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> received_tag = {};
  etl::copy_n(generated_tag.begin(), received_tag.size(), received_tag.begin());
  TEST_ASSERT_TRUE(rpc::security::handshake_verify_tag(kSecret, kHandshakeNonce,
                                                       received_tag));

  received_tag[0] ^= 0xFFU;
  TEST_ASSERT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce, received_tag));

  TEST_ASSERT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce, etl::span<const uint8_t>()));

  TEST_ASSERT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce,
      etl::span<const uint8_t>(received_tag.data(), received_tag.size() - 1U)));
}

void test_ct_memcmp_returns_nonzero_for_high_bit_mismatch(void) {
  constexpr etl::array<uint8_t, 4U> expected = {};
  etl::array<uint8_t, 4U> received = {};
  received[2] = 0x80U;

  TEST_ASSERT_EQUAL_INT(
      0, mbedtls_ct_memcmp(expected.data(), expected.data(), expected.size()));
  TEST_ASSERT_NOT_EQUAL(
      0, mbedtls_ct_memcmp(expected.data(), received.data(), received.size()));
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
  TEST_ASSERT_FALSE(
      etl::equal(actual_tag.begin(), actual_tag.end(), response_tag.begin()));
}

void test_session_key_derivation_known_answer(void) {
  constexpr etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> kExpectedHostToMcu = {
      0x5D, 0xFD, 0x05, 0x83, 0x21, 0x1B, 0x31, 0x36, 0x0B, 0x59, 0x8B,
      0xC3, 0x79, 0xB2, 0xE4, 0x98, 0x6B, 0xFA, 0xC0, 0x8D, 0x3C, 0xEA,
      0x83, 0xCF, 0x2D, 0xE7, 0xE7, 0x1A, 0xC2, 0x69, 0xDB, 0xAB};
  constexpr etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> kExpectedMcuToHost = {
      0x6F, 0x91, 0xBF, 0x7D, 0x92, 0x38, 0x7A, 0x46, 0x0F, 0xE1, 0x37,
      0x37, 0xA3, 0x51, 0xF3, 0x3C, 0xF6, 0x3D, 0xFE, 0x26, 0xB2, 0xBC,
      0x68, 0x51, 0xA7, 0x80, 0x2C, 0x91, 0xF4, 0x3F, 0xEB, 0xBC};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> host_to_mcu = {};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> mcu_to_host = {};
  TEST_ASSERT_TRUE(rpc::security::derive_session_keys(
      kVectorSecret, kVectorNonce, host_to_mcu, mcu_to_host));
  TEST_ASSERT_EQUAL_UINT8_ARRAY(kExpectedHostToMcu.data(), host_to_mcu.data(),
                                host_to_mcu.size());
  TEST_ASSERT_EQUAL_UINT8_ARRAY(kExpectedMcuToHost.data(), mcu_to_host.data(),
                                mcu_to_host.size());
}

void test_psa_random_fails_without_entropy_source(void) {
  uint8_t random_byte = 0U;
  TEST_ASSERT_EQUAL_INT(PSA_SUCCESS, psa_crypto_init());
  TEST_ASSERT_EQUAL_INT(PSA_ERROR_INSUFFICIENT_ENTROPY,
                        psa_generate_random(&random_byte, sizeof(random_byte)));
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
  RUN_TEST(test_ct_memcmp_returns_nonzero_for_high_bit_mismatch);
  RUN_TEST(test_handshake_tag_known_answer);
  RUN_TEST(test_session_key_derivation_known_answer);
  RUN_TEST(test_psa_random_fails_without_entropy_source);
#if BRIDGE_ENABLE_POST_TESTS
  RUN_TEST(test_cryptographic_self_tests_run);
#endif
  return UNITY_END();
}
