/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include <unity.h>
#include <etl/array.h>
#include <etl/span.h>

#include "../src/config/bridge_config.h"
#include "../src/security/security.h"

#ifndef RPC_NONCE_COUNTER_MASK
#define RPC_NONCE_COUNTER_MASK 0xFFFFFFFFFFFFFFFFULL
#endif

void setUp(void) {}
void tearDown(void) {}

void test_bridge_nonce_overflow_protection() {
  // 1. Configurar el contador de nonce en el penúltimo valor posible (2^64 - 2)
  uint64_t nonce_counter = RPC_NONCE_COUNTER_MASK - 1;

  constexpr uint16_t cmd_id = 0x0001;
  constexpr uint16_t seq_id = 0x0001;
  static constexpr etl::array<uint8_t, 32> test_key = {
      {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10,
       0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20}};
  static constexpr etl::array<uint8_t, 8> payload = {
      {'T', 'E', 'S', 'T', 'D', 'A', 'T', 'A'}};

  etl::array<uint8_t, 8> out_payload = {};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> out_nonce = {};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> out_tag = {};

  // 2. Primera emisión: Debe ser EXITOSA y llevar el contador al límite máximo (UINT64_MAX)
  bool status1 = rpc::security::aead_encrypt_frame(
      cmd_id, seq_id,
      etl::span<const uint8_t>(payload.data(), payload.size()),
      etl::span<const uint8_t>(test_key.data(), test_key.size()),
      &nonce_counter,
      etl::span<uint8_t>(out_payload.data(), out_payload.size()),
      etl::span<uint8_t>(out_nonce.data(), out_nonce.size()),
      etl::span<uint8_t>(out_tag.data(), out_tag.size()));

  TEST_ASSERT_TRUE_MESSAGE(status1, "Encryption failed at valid boundary counter (UINT64_MAX - 1)");
  TEST_ASSERT_EQUAL_UINT64(RPC_NONCE_COUNTER_MASK, nonce_counter);

  // 3. Segunda emisión: Debe SER RECHAZADA (retornar false) para evitar el wrap-around a 0
  bool status2 = rpc::security::aead_encrypt_frame(
      cmd_id, seq_id,
      etl::span<const uint8_t>(payload.data(), payload.size()),
      etl::span<const uint8_t>(test_key.data(), test_key.size()),
      &nonce_counter,
      etl::span<uint8_t>(out_payload.data(), out_payload.size()),
      etl::span<uint8_t>(out_nonce.data(), out_nonce.size()),
      etl::span<uint8_t>(out_tag.data(), out_tag.size()));

  TEST_ASSERT_FALSE_MESSAGE(status2, "CRITICAL: Nonce counter wrap-around was allowed past UINT64_MAX!");

  // 4. Verificar que el contador permanece congelado en UINT64_MAX y NO se reinició en 0
  TEST_ASSERT_EQUAL_UINT64(RPC_NONCE_COUNTER_MASK, nonce_counter);
}

void test_cryptographic_self_tests_run() {
#if BRIDGE_ENABLE_POST_TESTS
  bool post_res = rpc::security::run_cryptographic_self_tests();
  TEST_ASSERT_TRUE_MESSAGE(post_res, "Cryptographic POST (SHA256, HMAC, ChaCha20Poly1305 KATs) failed!");
#endif
}

int main(int argc, char** argv) {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_nonce_overflow_protection);
  RUN_TEST(test_cryptographic_self_tests_run);
  return UNITY_END();
}
  UNITY_BEGIN();
  RUN_TEST(test_bridge_nonce_overflow_protection);
  RUN_TEST(test_cryptographic_self_tests_run);
  return UNITY_END();
}
