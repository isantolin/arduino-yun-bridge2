/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include <etl/algorithm.h>
#include <string.h>
#include <etl/array.h>
#include <pw_span/span.h>

#if defined(min)
#undef min
#endif
#if defined(max)
#undef max
#endif

#include "pw_unit_test/framework.h"
#include <wolfssl/wolfcrypt/misc.h>
#include <wolfssl/wolfcrypt/settings.h>

#include "../src/security/security.h"
#include "protocol/rpc_protocol.h"

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
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH>
    kLongHandshakeNonce = {0U, 1U, 2U,  3U,  4U,  5U,  6U,  7U,
                           8U, 9U, 10U, 11U, 12U, 13U, 14U, 15U};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> kExpectedTag = {
    0xF0U, 0x7AU, 0x5AU, 0xA1U, 0x34U, 0x05U, 0x71U, 0x5BU,
    0x8B,  0x54U, 0xEFU, 0xDEU, 0x57U, 0xACU, 0x81U, 0x30U};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH>
    kExpectedLongHandshakeTag = {0x76U, 0x8DU, 0xFCU, 0xD9U, 0x27U, 0x29U,
                                 0xAEU, 0x57U, 0xC1U, 0x2AU, 0xEAU, 0xFDU,
                                 0x15U, 0x28U, 0x83U, 0xF8U};
// Handshake v2 (ADR-0005): MCU nonces bound into the response tag and keys.
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> kVectorMcuNonce =
    {0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU,
     0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU, 0xCDU};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> kLongMcuNonce = {
    0x10U, 0x11U, 0x12U, 0x13U, 0x14U, 0x15U, 0x16U, 0x17U,
    0x18U, 0x19U, 0x1AU, 0x1BU, 0x1CU, 0x1DU, 0x1EU, 0x1FU};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH>
    kExpectedLongHandshakeResponseTag = {
        0xFEU, 0xA1U, 0x21U, 0x43U, 0xFDU, 0x2AU, 0xEAU, 0x9EU,
        0xBEU, 0xA2U, 0x2EU, 0x7BU, 0xE3U, 0x00U, 0x1DU, 0x61U};
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH>
    kExpectedResponseTag = {0x31U, 0x51U, 0xD9U, 0xCEU, 0x73U, 0x2DU,
                            0xEAU, 0x90U, 0x56U, 0x03U, 0x1AU, 0x4CU,
                            0x4AU, 0x33U, 0xBEU, 0x70U};
// derive_mcu_nonce(kVectorSecret, all-zero counter block).
constexpr etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH>
    kExpectedMcuNonceZeroCounter = {0x3AU, 0xBCU, 0xD1U, 0x2CU, 0x55U, 0xA6U,
                                    0x7DU, 0x46U, 0x85U, 0x94U, 0xB1U, 0xAEU,
                                    0x91U, 0xCCU, 0x5DU, 0x44U};

}  // namespace

TEST(ArduinoCryptoHarden, HandshakeTagGenerationRejectsInvalidBuffers) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> output_tag = {0xAAU};
  const bool empty_secret_result = rpc::security::handshake_generate_tag(
      pw::span<const uint8_t>(), kHandshakeNonce, output_tag);
  EXPECT_FALSE(empty_secret_result);
  for (uint8_t byte : output_tag) {
    EXPECT_EQ(0U, byte);
  }

  output_tag.fill(0x55U);
  const bool short_nonce_result = rpc::security::handshake_generate_tag(
      kSecret,
      pw::span<const uint8_t>(kHandshakeNonce.data(),
                              kHandshakeNonce.size() - 1U),
      output_tag);
  EXPECT_FALSE(short_nonce_result);
  for (uint8_t byte : output_tag) {
    EXPECT_EQ(0U, byte);
  }

  output_tag.fill(0x77U);
  const bool short_tag_result = rpc::security::handshake_generate_tag(
      kSecret, kHandshakeNonce,
      pw::span<uint8_t>(output_tag.data(), output_tag.size() - 1U));
  EXPECT_FALSE(short_tag_result);
}

TEST(ArduinoCryptoHarden, HandshakeTagGenerationAndVerification) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> generated_tag = {};
  EXPECT_TRUE(rpc::security::handshake_generate_tag(
      kSecret, kHandshakeNonce, generated_tag));

  EXPECT_TRUE(rpc::security::handshake_verify_tag(kSecret, kHandshakeNonce,
                                                       generated_tag));

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> tampered_tag =
      generated_tag;
  tampered_tag[0] ^= 0x01U;
  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce, tampered_tag));

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> modified_nonce =
      kHandshakeNonce;
  modified_nonce[modified_nonce.size() - 1U] ^= 0x80U;
  EXPECT_FALSE(rpc::security::handshake_verify_tag(kSecret, modified_nonce,
                                                        generated_tag));

  constexpr etl::array<uint8_t, 13U> kWrongSecret = {
      {'w', 'r', 'o', 'n', 'g', '-', 's', 'e', 'c', 'r', 'e', 't', '!'}};
  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      kWrongSecret, kHandshakeNonce, generated_tag));

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> received_tag = {};
  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      pw::span<const uint8_t>(), kHandshakeNonce, received_tag));
  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      kSecret,
      pw::span<const uint8_t>(kHandshakeNonce.data(),
                              kHandshakeNonce.size() - 1U),
      received_tag));
  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, kHandshakeNonce,
      pw::span<const uint8_t>(received_tag.data(), received_tag.size() - 1U)));
}

TEST(ArduinoCryptoHarden, CtMemcmpReturnsNonzeroForHighBitMismatch) {
  constexpr etl::array<uint8_t, 4U> expected = {};
  etl::array<uint8_t, 4U> received = {};
  received[2] = 0x80U;

  EXPECT_EQ(0, ConstantCompare(expected.data(), expected.data(), expected.size()));
  EXPECT_NE(0, ConstantCompare(expected.data(), received.data(), received.size()));
}

TEST(ArduinoCryptoHarden, HandshakeTagKnownAnswer) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> actual_tag = {};
  EXPECT_TRUE(rpc::security::handshake_generate_tag(
      kVectorSecret, kVectorNonce, actual_tag));
  EXPECT_EQ(0, memcmp(kExpectedTag.data(), actual_tag.data(), kExpectedTag.size())));
  EXPECT_TRUE(rpc::security::handshake_verify_tag(
      kVectorSecret, kVectorNonce, kExpectedTag));

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> response_tag = {};
  EXPECT_TRUE(rpc::security::handshake_generate_response_tag(
      kVectorSecret, kVectorNonce, kVectorMcuNonce, response_tag));
  EXPECT_EQ(0, memcmp(kExpectedResponseTag.data(), response_tag.data(), response_tag.size())));
  EXPECT_FALSE(etl::equal(actual_tag.begin(), actual_tag.end(), response_tag.begin()));
}

TEST(ArduinoCryptoHarden, LongHandshakeSecretKnownAnswer) {
  etl::array<uint8_t, 64U> secret = {};
  secret.fill(static_cast<uint8_t>('A'));
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> request_tag = {};
  EXPECT_TRUE(rpc::security::handshake_generate_tag(
      secret, kLongHandshakeNonce, request_tag));
  EXPECT_EQ(0, memcmp(kExpectedLongHandshakeTag.data(), request_tag.data(), request_tag.size())));
  EXPECT_TRUE(rpc::security::handshake_verify_tag(
      secret, kLongHandshakeNonce, kExpectedLongHandshakeTag));

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> response_tag = {};
  EXPECT_TRUE(rpc::security::handshake_generate_response_tag(
      secret, kLongHandshakeNonce, kLongMcuNonce, response_tag));
  EXPECT_EQ(0, memcmp(kExpectedLongHandshakeResponseTag.data(), response_tag.data(), response_tag.size())));

  constexpr etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> kExpectedHostToMcu = {
      0x36U, 0x53U, 0xCAU, 0x7EU, 0x5FU, 0x20U, 0xB5U, 0x58U,
      0xE5U, 0xA2U, 0x2BU, 0x50U, 0x6CU, 0xC8U, 0x57U, 0x96U,
      0x41U, 0x3EU, 0x82U, 0x9CU, 0xE2U, 0xD9U, 0x1FU, 0x8BU,
      0xF5U, 0x81U, 0xF3U, 0xF4U, 0xD7U, 0x02U, 0xB7U, 0x7FU};
  constexpr etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> kExpectedMcuToHost = {
      0xA3U, 0x97U, 0x06U, 0x4EU, 0x8AU, 0x86U, 0xCFU, 0x3EU,
      0x8EU, 0x8CU, 0xABU, 0x43U, 0x75U, 0xABU, 0x9BU, 0xF6U,
      0x4DU, 0xB7U, 0x01U, 0x9DU, 0x08U, 0x78U, 0x60U, 0x76U,
      0xF6U, 0xB5U, 0x57U, 0xF1U, 0xA4U, 0x7FU, 0x9CU, 0xBEU};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE * 2U> session_keys = {};
  EXPECT_TRUE(rpc::security::derive_session_keys(
      secret, kLongHandshakeNonce, kLongMcuNonce, session_keys));
  EXPECT_EQ(0, memcmp(kExpectedHostToMcu.data(), session_keys.data(), kExpectedHostToMcu.size())));
  EXPECT_EQ(0, memcmp(kExpectedMcuToHost.data(), session_keys.data() + kExpectedHostToMcu.size(), kExpectedMcuToHost.size())));
}

TEST(ArduinoCryptoHarden, SessionKeyDerivationKnownAnswer) {
  constexpr etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> kExpectedHostToMcu = {
      0xD3U, 0xE9U, 0x1DU, 0x2AU, 0x82U, 0xDAU, 0x25U, 0x7EU,
      0x8AU, 0x6AU, 0xABU, 0x60U, 0xB5U, 0x81U, 0xCBU, 0x33U,
      0xCCU, 0x95U, 0x3CU, 0x4AU, 0x3BU, 0xDEU, 0x54U, 0x8FU,
      0x7AU, 0x07U, 0xA8U, 0xE8U, 0xDBU, 0xC2U, 0x11U, 0x6BU};
  constexpr etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> kExpectedMcuToHost = {
      0x04U, 0x22U, 0x5CU, 0x74U, 0xDCU, 0xB3U, 0xF1U, 0xDEU,
      0x5DU, 0x00U, 0x0DU, 0xFAU, 0xECU, 0xA1U, 0xD8U, 0xBBU,
      0x6AU, 0x08U, 0x4EU, 0xB1U, 0x37U, 0x9DU, 0x63U, 0x55U,
      0x1CU, 0xC0U, 0x76U, 0x56U, 0x3AU, 0xE5U, 0x33U, 0xE8U};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE * 2U> session_keys = {};
  EXPECT_TRUE(rpc::security::derive_session_keys(
      kVectorSecret, kVectorNonce, kVectorMcuNonce, session_keys));
  EXPECT_EQ(0, memcmp(kExpectedHostToMcu.data(), session_keys.data(), kExpectedHostToMcu.size())));
  EXPECT_EQ(0, memcmp(kExpectedMcuToHost.data(), session_keys.data() + kExpectedHostToMcu.size(), kExpectedMcuToHost.size())));
}

TEST(ArduinoCryptoHarden, McuNonceKnownAnswerAndCounterUniqueness) {
  const etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> zero_counter{};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> nonce_a{};
  EXPECT_TRUE(rpc::security::derive_mcu_nonce(kVectorSecret, zero_counter, nonce_a));
  EXPECT_EQ(0, memcmp(kExpectedMcuNonceZeroCounter.data(), nonce_a.data(), nonce_a.size())));

  // A different handshake counter (last byte) must give a different nonce.
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> next_counter{};
  next_counter[rpc::RPC_HANDSHAKE_NONCE_LENGTH - 1U] = 1U;
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> nonce_b{};
  EXPECT_TRUE(rpc::security::derive_mcu_nonce(kVectorSecret, next_counter, nonce_b));
  EXPECT_FALSE(etl::equal(nonce_a.begin(), nonce_a.end(), nonce_b.begin()));
}

TEST(ArduinoCryptoHarden, CryptoPublicApiRejectsInvalidDimensions) {
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> key{};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE * 2U> session_keys{};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE * 2U - 1U> short_session_keys{};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE - 1U> short_key{};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> aead_nonce{};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE - 1U> short_aead_nonce{};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag{};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE - 1U> short_aead_tag{};
  etl::array<uint8_t, 8U> input{};
  etl::array<uint8_t, 8U> output{};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> handshake_nonce{};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH - 1U>
      short_handshake_nonce{};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> handshake_tag{};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH - 1U> short_handshake_tag{};

  EXPECT_FALSE(rpc::security::derive_session_keys(
      pw::span<const uint8_t>(), handshake_nonce, handshake_nonce,
      session_keys));
  EXPECT_FALSE(rpc::security::derive_session_keys(
      kSecret, short_handshake_nonce, handshake_nonce, session_keys));
  EXPECT_FALSE(rpc::security::derive_session_keys(
      kSecret, handshake_nonce, short_handshake_nonce, session_keys));
  EXPECT_FALSE(rpc::security::derive_session_keys(
      kSecret, handshake_nonce, handshake_nonce, short_session_keys));

  EXPECT_FALSE(rpc::security::handshake_generate_tag(
      pw::span<const uint8_t>(), handshake_nonce, handshake_tag));
  EXPECT_FALSE(rpc::security::handshake_generate_tag(
      kSecret, short_handshake_nonce, handshake_tag));
  EXPECT_FALSE(rpc::security::handshake_generate_tag(
      kSecret, handshake_nonce, short_handshake_tag));

  EXPECT_FALSE(rpc::security::handshake_generate_response_tag(
      pw::span<const uint8_t>(), handshake_nonce, handshake_nonce,
      handshake_tag));
  EXPECT_FALSE(rpc::security::handshake_generate_response_tag(
      kSecret, short_handshake_nonce, handshake_nonce, handshake_tag));
  EXPECT_FALSE(rpc::security::handshake_generate_response_tag(
      kSecret, handshake_nonce, short_handshake_nonce, handshake_tag));
  EXPECT_FALSE(rpc::security::handshake_generate_response_tag(
      kSecret, handshake_nonce, handshake_nonce, short_handshake_tag));

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> mcu_nonce{};
  EXPECT_FALSE(rpc::security::derive_mcu_nonce(
      pw::span<const uint8_t>(), handshake_nonce, mcu_nonce));
  EXPECT_FALSE(rpc::security::derive_mcu_nonce(
      kSecret, short_handshake_nonce, mcu_nonce));
  EXPECT_FALSE(rpc::security::derive_mcu_nonce(
      kSecret, handshake_nonce,
      pw::span<uint8_t>(mcu_nonce.data(), mcu_nonce.size() - 1U)));

  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      pw::span<const uint8_t>(), handshake_nonce, handshake_tag));
  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, short_handshake_nonce, handshake_tag));
  EXPECT_FALSE(rpc::security::handshake_verify_tag(
      kSecret, handshake_nonce, short_handshake_tag));

  EXPECT_FALSE(rpc::security::aead_encrypt(
      short_key, aead_nonce, pw::span<const uint8_t>(), input, output, tag));
  EXPECT_FALSE(rpc::security::aead_encrypt(
      key, short_aead_nonce, pw::span<const uint8_t>(), input, output, tag));
  EXPECT_FALSE(rpc::security::aead_encrypt(key, aead_nonce,
                                                pw::span<const uint8_t>(),
                                                input, output, short_aead_tag));
  EXPECT_FALSE(rpc::security::aead_encrypt(
      key, aead_nonce, pw::span<const uint8_t>(), input,
      pw::span<uint8_t>(output.data(), input.size() - 1U), tag));

  EXPECT_FALSE(rpc::security::aead_decrypt(
      short_key, aead_nonce, pw::span<const uint8_t>(), input, tag, output));
  EXPECT_FALSE(rpc::security::aead_decrypt(
      key, short_aead_nonce, pw::span<const uint8_t>(), input, tag, output));
  EXPECT_FALSE(rpc::security::aead_decrypt(key, aead_nonce,
                                                pw::span<const uint8_t>(),
                                                input, short_aead_tag, output));
  EXPECT_FALSE(rpc::security::aead_decrypt(
      key, aead_nonce, pw::span<const uint8_t>(), input, tag,
      pw::span<uint8_t>(output.data(), input.size() - 1U)));
}

TEST(ArduinoCryptoHarden, AeadEmptyPayloadAndAadEdgeCases) {
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> key{};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce{};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag{};
  const uint8_t aad_byte = 1U;
  EXPECT_TRUE(rpc::security::aead_encrypt(
      key, nonce, pw::span<const uint8_t>(&aad_byte, 1U),
      pw::span<const uint8_t>(), pw::span<uint8_t>(), tag));
  EXPECT_TRUE(rpc::security::aead_decrypt(
      key, nonce, pw::span<const uint8_t>(&aad_byte, 1U),
      pw::span<const uint8_t>(), tag, pw::span<uint8_t>()));
  EXPECT_TRUE(rpc::security::aead_encrypt(
      key, nonce, pw::span<const uint8_t>(), pw::span<const uint8_t>(),
      pw::span<uint8_t>(), tag));
  EXPECT_TRUE(rpc::security::aead_decrypt(
      key, nonce, pw::span<const uint8_t>(), pw::span<const uint8_t>(), tag,
      pw::span<uint8_t>()));
}

namespace {

// Collects aead_encrypt_chunked() output and can fail on a chosen call.
struct ChunkCollector {
  etl::array<uint8_t,
             rpc::security::kAeadChunkedMaxPlaintext + rpc::RPC_AEAD_TAG_SIZE>
      bytes{};
  size_t size = 0U;
  size_t calls = 0U;
  size_t fail_on_call = 0U;  // 1-based; 0 never fails.

  bool append(pw::span<const uint8_t> chunk) {
    ++calls;
    if (calls == fail_on_call) return false;
    EXPECT_TRUE(size + chunk.size() <= bytes.size());
    etl::copy(chunk.begin(), chunk.end(), bytes.begin() + size);
    size += chunk.size();
    return true;
  }
};

void assert_chunked_matches_one_shot(pw::span<const uint8_t> aad,
                                     pw::span<const uint8_t> plaintext) {
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> key{};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce{};
  key.fill(0x42U);
  nonce.fill(0x07U);

  etl::array<uint8_t, 128> expected_ciphertext{};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> expected_tag{};
  EXPECT_TRUE(plaintext.size() <= expected_ciphertext.size());
  EXPECT_TRUE(rpc::security::aead_encrypt(
      key, nonce, aad, plaintext,
      pw::span<uint8_t>(expected_ciphertext.data(), plaintext.size()),
      expected_tag));

  ChunkCollector collector;
  auto sink_fn = [&collector](pw::span<const uint8_t> chunk) {
    return collector.append(chunk);
  };
  EXPECT_TRUE(rpc::security::aead_encrypt_chunked(
      key, nonce, aad, plaintext, rpc::security::AeadOutputSink(sink_fn)));

  EXPECT_EQ(static_cast<uint32_t>(plaintext.size() + rpc::RPC_AEAD_TAG_SIZE), static_cast<uint32_t>(collector.size));
  if (!plaintext.empty()) {
    EXPECT_EQ(0, memcmp(expected_ciphertext.data(), collector.bytes.data(), plaintext.size())));
  }
  EXPECT_EQ(0, memcmp(expected_tag.data(), collector.bytes.data() + plaintext.size(), expected_tag.size())));
}

}  // namespace

TEST(ArduinoCryptoHarden, AeadEncryptChunkedMatchesOneShot) {
  etl::array<uint8_t, 70> plaintext{};  // 32 + 32 + 6 byte chunks.
  for (size_t i = 0; i < plaintext.size(); ++i) {
    plaintext[i] = static_cast<uint8_t>(i * 13U + 1U);
  }
  etl::array<uint8_t, 9> aad{};
  aad.fill(0xA5U);

  assert_chunked_matches_one_shot(aad, plaintext);
  assert_chunked_matches_one_shot(pw::span<const uint8_t>(), plaintext);
  assert_chunked_matches_one_shot(aad, pw::span<const uint8_t>());
}

TEST(ArduinoCryptoHarden, AeadEncryptChunkedRejectsAndAborts) {
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> key{};
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE - 1U> short_key{};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce{};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE - 1U> short_nonce{};
  etl::array<uint8_t, 40> plaintext{};
  etl::array<uint8_t, rpc::security::kAeadChunkedMaxPlaintext + 1U> oversized{};

  ChunkCollector collector;
  auto sink_fn = [&collector](pw::span<const uint8_t> chunk) {
    return collector.append(chunk);
  };
  const rpc::security::AeadOutputSink sink(sink_fn);

  // Invalid inputs are rejected before any output is produced.
  EXPECT_FALSE(rpc::security::aead_encrypt_chunked(
      short_key, nonce, pw::span<const uint8_t>(), plaintext, sink));
  EXPECT_FALSE(rpc::security::aead_encrypt_chunked(
      key, short_nonce, pw::span<const uint8_t>(), plaintext, sink));
  EXPECT_FALSE(rpc::security::aead_encrypt_chunked(
      key, nonce, pw::span<const uint8_t>(), oversized, sink));
  EXPECT_FALSE(rpc::security::aead_encrypt_chunked(
      key, nonce, pw::span<const uint8_t>(), plaintext,
      rpc::security::AeadOutputSink()));
  EXPECT_EQ(0U, static_cast<uint32_t>(collector.calls));

  // A sink failure on the first ciphertext chunk aborts the operation.
  collector = ChunkCollector{};
  collector.fail_on_call = 1U;
  EXPECT_FALSE(rpc::security::aead_encrypt_chunked(
      key, nonce, pw::span<const uint8_t>(), plaintext, sink));
  EXPECT_EQ(1U, static_cast<uint32_t>(collector.calls));

  // 40 bytes -> two ciphertext chunks, then the tag on the third call.
  collector = ChunkCollector{};
  collector.fail_on_call = 3U;
  EXPECT_FALSE(rpc::security::aead_encrypt_chunked(
      key, nonce, pw::span<const uint8_t>(), plaintext, sink));
  EXPECT_EQ(3U, static_cast<uint32_t>(collector.calls));
}

#if BRIDGE_ENABLE_POST_TESTS
TEST(ArduinoCryptoHarden, CryptographicSelfTestsRun) {
  EXPECT_TRUE(rpc::security::run_cryptographic_self_tests());
}
#endif

