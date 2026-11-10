/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include "security.h"

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/string_view.h>
#include <mbedtls/platform_util.h>
#include <psa/crypto.h>
#include <psa/crypto_extra.h>
#include <pw_span/span.h>
#if defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_AVR)
#include <wolfssl/wolfcrypt/settings.h>
#endif
#if defined(ARDUINO_ARCH_ESP32)
#include <wolfssl/wolfcrypt/chacha20_poly1305.h>
#endif
#if defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_AVR)
#include <wolfssl/wolfcrypt/hmac.h>
#endif

#include "../config/bridge_config.h"

#if defined(MBEDTLS_PSA_CRYPTO_EXTERNAL_RNG)
/* AVR/SAMD have no cryptographic RNG; fail closed if PSA random is requested.
 */
extern "C" psa_status_t mbedtls_psa_external_get_random(
    mbedtls_psa_external_random_context_t* context, uint8_t* output,
    size_t output_size, size_t* output_length) {
  static_cast<void>(context);
  static_cast<void>(output);
  static_cast<void>(output_size);
  if (output_length == nullptr) {
    return PSA_ERROR_INVALID_ARGUMENT;
  }
  *output_length = 0U;
  return PSA_ERROR_INSUFFICIENT_ENTROPY;
}
#endif

namespace rpc {
namespace security {
namespace {

constexpr psa_algorithm_t kHmacSha256 = PSA_ALG_HMAC(PSA_ALG_SHA_256);
constexpr psa_algorithm_t kHandshakeMac = PSA_ALG_TRUNCATED_MAC(
    kHmacSha256, static_cast<psa_algorithm_t>(RPC_HANDSHAKE_TAG_LENGTH));
#if !defined(ARDUINO_ARCH_ESP32) && !defined(ARDUINO_ARCH_AVR)
constexpr psa_algorithm_t kHkdfSha256 = PSA_ALG_HKDF(PSA_ALG_SHA_256);
#endif
#if !defined(ARDUINO_ARCH_ESP32)
constexpr psa_algorithm_t kChaCha20Poly1305 = PSA_ALG_CHACHA20_POLY1305;
#endif
static_assert(PSA_MAC_LENGTH(PSA_KEY_TYPE_HMAC, 256U, kHandshakeMac) ==
              RPC_HANDSHAKE_TAG_LENGTH);
static uint8_t kEmptyBuffer = 0U;

#if defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_AVR)
bool fits_wolfssl_length(size_t length) {
  return length <= static_cast<size_t>(UINT32_MAX);
}
#endif

template <size_t N>
static etl::array<uint8_t, N> copy_progmem(const etl::array<uint8_t, N>& src) {
  etl::array<uint8_t, N> dst;
#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR)
  memcpy_P(dst.data(), src.data(), N);
#else
  etl::copy(src.begin(), src.end(), dst.begin());
#endif
  return dst;
}

void initialize_crypto() { static_cast<void>(psa_crypto_init()); }

mbedtls_svc_key_id_t import_key(psa_key_type_t type, size_t bits,
                                psa_key_usage_t usage,
                                psa_algorithm_t algorithm,
                                pw::span<const uint8_t> material) {
  mbedtls_svc_key_id_t key_id = 0;
  psa_key_attributes_t attributes = PSA_KEY_ATTRIBUTES_INIT;
  psa_set_key_type(&attributes, type);
  psa_set_key_bits(&attributes, bits);
  psa_set_key_lifetime(&attributes, PSA_KEY_LIFETIME_VOLATILE);
  psa_set_key_usage_flags(&attributes, usage);
  psa_set_key_algorithm(&attributes, algorithm);
  static_cast<void>(
      psa_import_key(&attributes, material.data(), material.size(), &key_id));
  psa_reset_key_attributes(&attributes);
  return key_id;
}

void derive_hkdf_sha256(pw::span<const uint8_t> secret,
                        pw::span<const uint8_t> salt,
                        pw::span<const uint8_t> info,
                        pw::span<uint8_t> output) {
#if defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_AVR)
  if (secret.empty() || output.empty() ||
      output.size() > 255U * RPC_SHA256_DIGEST_SIZE ||
      !fits_wolfssl_length(secret.size()) ||
      !fits_wolfssl_length(salt.size()) || !fits_wolfssl_length(info.size()) ||
      !fits_wolfssl_length(output.size())) {
    secure_zero(output);
    return;
  }

  const byte* salt_data = salt.empty() ? &kEmptyBuffer : salt.data();
  const byte* info_data = info.empty() ? &kEmptyBuffer : info.data();
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> pseudo_random_key = {};
  bool ok =
      wc_HKDF_Extract(WC_SHA256, salt_data, static_cast<word32>(salt.size()),
                      secret.data(), static_cast<word32>(secret.size()),
                      pseudo_random_key.data()) == 0;
  if (ok) {
    ok = wc_HKDF_Expand(WC_SHA256, pseudo_random_key.data(),
                        static_cast<word32>(pseudo_random_key.size()),
                        info_data, static_cast<word32>(info.size()),
                        output.data(), static_cast<word32>(output.size())) == 0;
  }
  secure_zero(pw::span<uint8_t>(pseudo_random_key));
  if (!ok) {
    secure_zero(output);
  }
#else
  initialize_crypto();
  const mbedtls_svc_key_id_t secret_key =
      import_key(PSA_KEY_TYPE_DERIVE, secret.size() * 8U, PSA_KEY_USAGE_DERIVE,
                 kHkdfSha256, secret);
  psa_key_derivation_operation_t operation = PSA_KEY_DERIVATION_OPERATION_INIT;
  static_cast<void>(psa_key_derivation_setup(&operation, kHkdfSha256));
  static_cast<void>(psa_key_derivation_input_bytes(
      &operation, PSA_KEY_DERIVATION_INPUT_SALT, salt.data(), salt.size()));
  static_cast<void>(psa_key_derivation_input_key(
      &operation, PSA_KEY_DERIVATION_INPUT_SECRET, secret_key));
  static_cast<void>(psa_key_derivation_input_bytes(
      &operation, PSA_KEY_DERIVATION_INPUT_INFO, info.data(), info.size()));
  static_cast<void>(psa_key_derivation_output_bytes(&operation, output.data(),
                                                    output.size()));
  static_cast<void>(psa_key_derivation_abort(&operation));
  static_cast<void>(psa_destroy_key(secret_key));
#endif
}

void compute_mac(pw::span<const uint8_t> key, psa_algorithm_t algorithm,
                 pw::span<const uint8_t> input, pw::span<uint8_t> output) {
  initialize_crypto();
  const mbedtls_svc_key_id_t key_id =
      import_key(PSA_KEY_TYPE_HMAC, key.size() * 8U, PSA_KEY_USAGE_SIGN_MESSAGE,
                 algorithm, key);
  size_t output_length = 0;
  static_cast<void>(psa_mac_compute(key_id, algorithm, input.data(),
                                    input.size(), output.data(), output.size(),
                                    &output_length));
  static_cast<void>(psa_destroy_key(key_id));
}

bool verify_mac(pw::span<const uint8_t> key, psa_algorithm_t algorithm,
                pw::span<const uint8_t> input,
                pw::span<const uint8_t> expected_mac) {
  initialize_crypto();
  const mbedtls_svc_key_id_t key_id =
      import_key(PSA_KEY_TYPE_HMAC, key.size() * 8U,
                 PSA_KEY_USAGE_VERIFY_MESSAGE, algorithm, key);
  const psa_status_t status =
      psa_mac_verify(key_id, algorithm, input.data(), input.size(),
                     expected_mac.data(), expected_mac.size());
  static_cast<void>(psa_destroy_key(key_id));
  return status == PSA_SUCCESS;
}

void derive_handshake_auth_key(pw::span<const uint8_t> secret,
                               pw::span<const uint8_t> info,
                               pw::span<uint8_t> auth_key) {
  const auto salt = copy_progmem(RPC_HANDSHAKE_HKDF_SALT);
  derive_hkdf_sha256(secret, salt, info, auth_key);
}

bool derive_handshake_tag(pw::span<const uint8_t> secret,
                          pw::span<const uint8_t> nonce,
                          pw::span<const uint8_t> info,
                          pw::span<uint8_t> out_tag) {
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH ||
      out_tag.size() < RPC_HANDSHAKE_TAG_LENGTH) {
    secure_zero(out_tag);
    return false;
  }

  secure_zero(out_tag);
  etl::array<uint8_t, RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> auth_key = {};
  derive_handshake_auth_key(secret, info, auth_key);
  compute_mac(auth_key, kHandshakeMac, nonce,
              pw::span<uint8_t>(out_tag.data(), RPC_HANDSHAKE_TAG_LENGTH));
  secure_zero(pw::span<uint8_t>(auth_key));
  return true;
}

#if BRIDGE_ENABLE_POST_TESTS
bool combine_ok(bool lhs, bool rhs) {
  return (static_cast<unsigned>(lhs) & static_cast<unsigned>(rhs)) != 0U;
}
#endif

}  // namespace

void secure_zero(pw::span<uint8_t> buf) {
  if (!buf.empty()) {
    mbedtls_platform_zeroize(buf.data(), buf.size());
  }
}

bool derive_session_keys(pw::span<const uint8_t> secret,
                         pw::span<const uint8_t> nonce,
                         pw::span<uint8_t> host_to_mcu_key,
                         pw::span<uint8_t> mcu_to_host_key) {
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH ||
      host_to_mcu_key.size() != RPC_AEAD_KEY_SIZE ||
      mcu_to_host_key.size() != RPC_AEAD_KEY_SIZE) {
    secure_zero(host_to_mcu_key);
    secure_zero(mcu_to_host_key);
    return false;
  }

  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_SESSION);
  etl::array<uint8_t, RPC_AEAD_KEY_SIZE * 2U> keys = {};
  derive_hkdf_sha256(secret, nonce, info, pw::span<uint8_t>(keys));
  etl::copy_n(keys.begin(), host_to_mcu_key.size(), host_to_mcu_key.begin());
  etl::copy_n(keys.begin() + host_to_mcu_key.size(), mcu_to_host_key.size(),
              mcu_to_host_key.begin());
  secure_zero(pw::span<uint8_t>(keys));
  return true;
}

bool aead_encrypt(pw::span<const uint8_t> key, pw::span<const uint8_t> nonce,
                  pw::span<const uint8_t> aad,
                  pw::span<const uint8_t> plaintext,
                  pw::span<uint8_t> ciphertext, pw::span<uint8_t> tag) {
  if (key.size() != RPC_AEAD_KEY_SIZE || nonce.size() != RPC_AEAD_NONCE_SIZE ||
      ciphertext.size() < plaintext.size() || tag.size() != RPC_AEAD_TAG_SIZE) {
    secure_zero(ciphertext);
    secure_zero(tag);
    return false;
  }

#if defined(ARDUINO_ARCH_ESP32)
  const uint8_t* aad_data = aad.empty() ? &kEmptyBuffer : aad.data();
  const uint8_t* plaintext_data =
      plaintext.empty() ? &kEmptyBuffer : plaintext.data();
  uint8_t* ciphertext_data =
      ciphertext.empty() ? &kEmptyBuffer : ciphertext.data();
  const bool ok =
      fits_wolfssl_length(aad.size()) &&
      fits_wolfssl_length(plaintext.size()) &&
      wc_ChaCha20Poly1305_Encrypt(
          key.data(), nonce.data(), aad_data, static_cast<word32>(aad.size()),
          plaintext_data, static_cast<word32>(plaintext.size()),
          ciphertext_data, tag.data()) == 0;
  if (!ok) {
    secure_zero(ciphertext);
    secure_zero(tag);
  }
  return ok;
#else
  initialize_crypto();
  const mbedtls_svc_key_id_t key_id =
      import_key(PSA_KEY_TYPE_CHACHA20, key.size() * 8U, PSA_KEY_USAGE_ENCRYPT,
                 kChaCha20Poly1305, key);
  psa_aead_operation_t operation = PSA_AEAD_OPERATION_INIT;
  static_cast<void>(
      psa_aead_encrypt_setup(&operation, key_id, kChaCha20Poly1305));
  static_cast<void>(psa_aead_set_nonce(&operation, nonce.data(), nonce.size()));
  if (!aad.empty()) {
    static_cast<void>(psa_aead_update_ad(&operation, aad.data(), aad.size()));
  }

  size_t update_length = 0;
  if (!plaintext.empty()) {
    static_cast<void>(psa_aead_update(&operation, plaintext.data(),
                                      plaintext.size(), ciphertext.data(),
                                      ciphertext.size(), &update_length));
  }

  size_t finish_length = 0;
  size_t tag_length = 0;
  static_cast<void>(psa_aead_finish(&operation, &kEmptyBuffer, 0U,
                                    &finish_length, tag.data(), tag.size(),
                                    &tag_length));
  static_cast<void>(psa_aead_abort(&operation));
  static_cast<void>(psa_destroy_key(key_id));
  return true;
#endif
}

bool aead_decrypt(pw::span<const uint8_t> key, pw::span<const uint8_t> nonce,
                  pw::span<const uint8_t> aad,
                  pw::span<const uint8_t> ciphertext,
                  pw::span<const uint8_t> tag, pw::span<uint8_t> plaintext) {
  if (key.size() != RPC_AEAD_KEY_SIZE || nonce.size() != RPC_AEAD_NONCE_SIZE ||
      ciphertext.size() > plaintext.size() || tag.size() != RPC_AEAD_TAG_SIZE) {
    secure_zero(plaintext);
    return false;
  }

#if defined(ARDUINO_ARCH_ESP32)
  const uint8_t* aad_data = aad.empty() ? &kEmptyBuffer : aad.data();
  const uint8_t* ciphertext_data =
      ciphertext.empty() ? &kEmptyBuffer : ciphertext.data();
  uint8_t* plaintext_data =
      plaintext.empty() ? &kEmptyBuffer : plaintext.data();
  const bool ok =
      fits_wolfssl_length(aad.size()) &&
      fits_wolfssl_length(ciphertext.size()) &&
      wc_ChaCha20Poly1305_Decrypt(
          key.data(), nonce.data(), aad_data, static_cast<word32>(aad.size()),
          ciphertext_data, static_cast<word32>(ciphertext.size()), tag.data(),
          plaintext_data) == 0;
#else
  initialize_crypto();
  const mbedtls_svc_key_id_t key_id =
      import_key(PSA_KEY_TYPE_CHACHA20, key.size() * 8U, PSA_KEY_USAGE_DECRYPT,
                 kChaCha20Poly1305, key);
  psa_aead_operation_t operation = PSA_AEAD_OPERATION_INIT;
  static_cast<void>(
      psa_aead_decrypt_setup(&operation, key_id, kChaCha20Poly1305));
  static_cast<void>(psa_aead_set_nonce(&operation, nonce.data(), nonce.size()));
  if (!aad.empty()) {
    static_cast<void>(psa_aead_update_ad(&operation, aad.data(), aad.size()));
  }

  size_t update_length = 0;
  if (!ciphertext.empty()) {
    static_cast<void>(psa_aead_update(&operation, ciphertext.data(),
                                      ciphertext.size(), plaintext.data(),
                                      plaintext.size(), &update_length));
  }

  size_t verify_length = 0;
  const bool ok = psa_aead_verify(&operation, &kEmptyBuffer, 0U, &verify_length,
                                  tag.data(), tag.size()) == PSA_SUCCESS;
  static_cast<void>(psa_aead_abort(&operation));
  static_cast<void>(psa_destroy_key(key_id));
#endif
  if (!ok) {
    secure_zero(plaintext);
  }
  return ok;
}

bool handshake_generate_tag(pw::span<const uint8_t> secret,
                            pw::span<const uint8_t> nonce,
                            pw::span<uint8_t> out_tag) {
  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_AUTH);
  return derive_handshake_tag(secret, nonce, info, out_tag);
}

bool handshake_generate_response_tag(pw::span<const uint8_t> secret,
                                     pw::span<const uint8_t> nonce,
                                     pw::span<uint8_t> out_tag) {
  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_RESPONSE);
  return derive_handshake_tag(secret, nonce, info, out_tag);
}

bool handshake_verify_tag(pw::span<const uint8_t> secret,
                          pw::span<const uint8_t> nonce,
                          pw::span<const uint8_t> received_tag) {
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH ||
      received_tag.size() != RPC_HANDSHAKE_TAG_LENGTH) {
    return false;
  }

  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_AUTH);
  etl::array<uint8_t, RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> auth_key = {};
  derive_handshake_auth_key(secret, info, auth_key);
  const bool ok = verify_mac(auth_key, kHandshakeMac, nonce, received_tag);
  secure_zero(pw::span<uint8_t>(auth_key));
  return ok;
}

#if BRIDGE_ENABLE_POST_TESTS

static constexpr etl::array<uint8_t, 3> kat_sha256_msg PROGMEM = {
    {'a', 'b', 'c'}};
static constexpr etl::array<uint8_t, 32> kat_sha256_expected PROGMEM = {
    {0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
     0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
     0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD}};
static constexpr etl::array<uint8_t, 3> kat_hmac_key PROGMEM = {
    {'k', 'e', 'y'}};
static constexpr etl::array<uint8_t, 56> kat_hmac_data PROGMEM = {
    {'J', 'o', 'v', 'e', 'n',  'c',  'i', 'l', 'l', 'o',  ' ',  'e', 'm', 'p',
     'o', 'n', 'z', 'o', 0xC3, 0xB1, 'a', 'd', 'o', ' ',  'd',  'e', ' ', 'w',
     'h', 'i', 's', 'k', 'y',  ',',  ' ', 'q', 'u', 0xC3, 0xA9, ' ', 'f', 'i',
     'g', 'u', 'r', 'i', 't',  'a',  's', ' ', 'e', 'x',  'h',  'i', 'b', 'e'}};
static constexpr etl::array<uint8_t, 32> kat_hmac_expected PROGMEM = {
    {0x53, 0x75, 0x96, 0x3F, 0x9E, 0x70, 0x9B, 0x58, 0x41, 0x50, 0x41,
     0xBA, 0xD2, 0xD4, 0x4D, 0xE2, 0x1F, 0x50, 0x80, 0x0E, 0x08, 0x41,
     0xB8, 0x7E, 0x0D, 0xAD, 0xFC, 0xDF, 0xE3, 0x62, 0xB2, 0x6C}};
static constexpr etl::array<uint8_t, 10> kat_handshake_secret PROGMEM = {
    {'s', 'h', 'a', 'r', 'e', 'd', '-', 'k', 'e', 'y'}};
static constexpr etl::array<uint8_t, RPC_HANDSHAKE_NONCE_LENGTH>
    kat_handshake_nonce PROGMEM = {{0xAB, 0xAB, 0xAB, 0xAB, 0xAB, 0xAB, 0xAB,
                                    0xAB, 0xAB, 0xAB, 0xAB, 0xAB, 0xAB, 0xAB,
                                    0xAB, 0xAB}};
static constexpr etl::array<uint8_t, RPC_HANDSHAKE_TAG_LENGTH>
    kat_handshake_request_tag PROGMEM = {{0xF0, 0x7A, 0x5A, 0xA1, 0x34, 0x05,
                                          0x71, 0x5B, 0x8B, 0x54, 0xEF, 0xDE,
                                          0x57, 0xAC, 0x81, 0x30}};
static constexpr etl::array<uint8_t, RPC_HANDSHAKE_TAG_LENGTH>
    kat_handshake_response_tag PROGMEM = {{0x46, 0x82, 0x99, 0xFA, 0xC4, 0xFB,
                                           0x82, 0xDE, 0x13, 0x91, 0xF8, 0xA9,
                                           0xA8, 0x4B, 0xAF, 0xD5}};
static constexpr etl::array<uint8_t, RPC_AEAD_KEY_SIZE> kat_aead_key PROGMEM = {
    {0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A,
     0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x90, 0x91, 0x92, 0x93, 0x94, 0x95,
     0x96, 0x97, 0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x9E, 0x9F}};
static constexpr etl::array<uint8_t, RPC_AEAD_NONCE_SIZE> kat_aead_nonce
    PROGMEM = {{0x07, 0x00, 0x00, 0x00, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45,
                0x46, 0x47}};
static constexpr etl::array<uint8_t, 12> kat_aead_aad PROGMEM = {
    {0x50, 0x51, 0x52, 0x53, 0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7}};
static constexpr etl::array<uint8_t, 4> kat_aead_plaintext PROGMEM = {
    {'t', 'e', 's', 't'}};
static constexpr etl::array<uint8_t, 4> kat_aead_ciphertext PROGMEM = {
    {0xEB, 0x1E, 0x9A, 0x29}};
static constexpr etl::array<uint8_t, RPC_AEAD_TAG_SIZE> kat_aead_tag PROGMEM = {
    {0x7D, 0xCA, 0x84, 0x79, 0x78, 0x7A, 0x5C, 0x19, 0x0F, 0x58, 0xEE, 0xDA,
     0xE6, 0xA0, 0x6B, 0xCF}};

bool run_cryptographic_self_tests() {
  initialize_crypto();
  const auto sha_msg = copy_progmem(kat_sha256_msg);
  const auto sha_expected = copy_progmem(kat_sha256_expected);
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> digest = {};
  size_t digest_length = 0;
  static_cast<void>(psa_hash_compute(PSA_ALG_SHA_256, sha_msg.data(),
                                     sha_msg.size(), digest.data(),
                                     digest.size(), &digest_length));
  bool ok = etl::equal(digest.begin(), digest.end(), sha_expected.begin());

  const auto hmac_key = copy_progmem(kat_hmac_key);
  const auto hmac_data = copy_progmem(kat_hmac_data);
  const auto hmac_expected = copy_progmem(kat_hmac_expected);
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> hmac_result = {};
  compute_mac(hmac_key, kHmacSha256, hmac_data, hmac_result);
  ok = combine_ok(ok, etl::equal(hmac_result.begin(), hmac_result.end(),
                                 hmac_expected.begin()));

  const auto handshake_secret = copy_progmem(kat_handshake_secret);
  const auto handshake_nonce = copy_progmem(kat_handshake_nonce);
  const auto handshake_request_expected =
      copy_progmem(kat_handshake_request_tag);
  const auto handshake_response_expected =
      copy_progmem(kat_handshake_response_tag);
  etl::array<uint8_t, RPC_HANDSHAKE_TAG_LENGTH> handshake_request = {};
  etl::array<uint8_t, RPC_HANDSHAKE_TAG_LENGTH> handshake_response = {};
  ok = combine_ok(ok, handshake_generate_tag(handshake_secret, handshake_nonce,
                                             handshake_request));
  ok = combine_ok(ok,
                  etl::equal(handshake_request.begin(), handshake_request.end(),
                             handshake_request_expected.begin()));
  ok = combine_ok(ok, handshake_verify_tag(handshake_secret, handshake_nonce,
                                           handshake_request_expected));
  ok = combine_ok(
      ok, handshake_generate_response_tag(handshake_secret, handshake_nonce,
                                          handshake_response));
  ok = combine_ok(
      ok, etl::equal(handshake_response.begin(), handshake_response.end(),
                     handshake_response_expected.begin()));

  const auto aead_key = copy_progmem(kat_aead_key);
  const auto aead_nonce = copy_progmem(kat_aead_nonce);
  const auto aead_aad = copy_progmem(kat_aead_aad);
  const auto aead_plaintext = copy_progmem(kat_aead_plaintext);
  const auto aead_expected = copy_progmem(kat_aead_ciphertext);
  const auto aead_expected_tag = copy_progmem(kat_aead_tag);
  etl::array<uint8_t, 4> ciphertext = {};
  etl::array<uint8_t, RPC_AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, 4> decrypted = {};
  ok = combine_ok(ok, aead_encrypt(aead_key, aead_nonce, aead_aad,
                                   aead_plaintext, ciphertext, tag));
  ok = combine_ok(ok, etl::equal(ciphertext.begin(), ciphertext.end(),
                                 aead_expected.begin()));
  ok = combine_ok(
      ok, etl::equal(tag.begin(), tag.end(), aead_expected_tag.begin()));
  ok =
      combine_ok(ok, aead_decrypt(aead_key, aead_nonce, aead_aad, aead_expected,
                                  aead_expected_tag, decrypted));
  ok = combine_ok(ok, etl::equal(decrypted.begin(), decrypted.end(),
                                 aead_plaintext.begin()));

  auto invalid_tag = aead_expected_tag;
  invalid_tag[0] ^= 0x01U;
  etl::array<uint8_t, 4> rejected_plaintext = {0xAAU, 0xAAU, 0xAAU, 0xAAU};
  ok = combine_ok(ok,
                  !aead_decrypt(aead_key, aead_nonce, aead_aad, aead_expected,
                                invalid_tag, rejected_plaintext));
  const etl::array<uint8_t, 4> zero_plaintext = {};
  ok = combine_ok(
      ok, etl::equal(rejected_plaintext.begin(), rejected_plaintext.end(),
                     zero_plaintext.begin()));

  secure_zero(pw::span<uint8_t>(digest));
  secure_zero(pw::span<uint8_t>(hmac_result));
  secure_zero(pw::span<uint8_t>(handshake_request));
  secure_zero(pw::span<uint8_t>(handshake_response));
  secure_zero(pw::span<uint8_t>(ciphertext));
  secure_zero(pw::span<uint8_t>(tag));
  secure_zero(pw::span<uint8_t>(decrypted));
  secure_zero(pw::span<uint8_t>(rejected_plaintext));
  return ok;
}

#endif  // BRIDGE_ENABLE_POST_TESTS

}  // namespace security
}  // namespace rpc
