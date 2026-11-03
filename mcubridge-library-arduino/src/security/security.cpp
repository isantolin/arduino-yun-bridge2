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

constexpr psa_algorithm_t kHkdfSha256 = PSA_ALG_HKDF(PSA_ALG_SHA_256);
constexpr psa_algorithm_t kHmacSha256 = PSA_ALG_HMAC(PSA_ALG_SHA_256);
constexpr psa_algorithm_t kHandshakeMac = PSA_ALG_TRUNCATED_MAC(
    kHmacSha256, static_cast<psa_algorithm_t>(RPC_HANDSHAKE_TAG_LENGTH));
constexpr psa_algorithm_t kChaCha20Poly1305 = PSA_ALG_CHACHA20_POLY1305;
static_assert(PSA_MAC_LENGTH(PSA_KEY_TYPE_HMAC, 256U, kHandshakeMac) ==
              RPC_HANDSHAKE_TAG_LENGTH);
static uint8_t kEmptyBuffer = 0U;

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

bool initialize_crypto() { return psa_crypto_init() == PSA_SUCCESS; }

bool import_key(psa_key_type_t type, size_t bits, psa_key_usage_t usage,
                psa_algorithm_t algorithm, etl::span<const uint8_t> material,
                mbedtls_svc_key_id_t& key_id) {
  key_id = 0;
  psa_key_attributes_t attributes = PSA_KEY_ATTRIBUTES_INIT;
  psa_set_key_type(&attributes, type);
  psa_set_key_bits(&attributes, bits);
  psa_set_key_lifetime(&attributes, PSA_KEY_LIFETIME_VOLATILE);
  psa_set_key_usage_flags(&attributes, usage);
  psa_set_key_algorithm(&attributes, algorithm);
  const psa_status_t status =
      psa_import_key(&attributes, material.data(), material.size(), &key_id);
  psa_reset_key_attributes(&attributes);
  return status == PSA_SUCCESS;
}

bool derive_hkdf_sha256(etl::span<const uint8_t> secret,
                        etl::span<const uint8_t> salt,
                        etl::span<const uint8_t> info,
                        etl::span<uint8_t> output) {
  if (secret.empty() || output.empty() || !initialize_crypto()) {
    secure_zero(output);
    return false;
  }

  mbedtls_svc_key_id_t secret_key = 0;
  const bool key_imported =
      import_key(PSA_KEY_TYPE_DERIVE, secret.size() * 8U, PSA_KEY_USAGE_DERIVE,
                 kHkdfSha256, secret, secret_key);
  psa_key_derivation_operation_t operation = PSA_KEY_DERIVATION_OPERATION_INIT;
  bool operation_started = false;
  bool ok = key_imported;

  if (ok) {
    ok = psa_key_derivation_setup(&operation, kHkdfSha256) == PSA_SUCCESS;
    operation_started = ok;
  }
  if (ok) {
    const uint8_t* salt_data = salt.empty() ? &kEmptyBuffer : salt.data();
    ok = psa_key_derivation_input_bytes(&operation,
                                        PSA_KEY_DERIVATION_INPUT_SALT,
                                        salt_data, salt.size()) == PSA_SUCCESS;
  }
  if (ok) {
    ok = psa_key_derivation_input_key(&operation,
                                      PSA_KEY_DERIVATION_INPUT_SECRET,
                                      secret_key) == PSA_SUCCESS;
  }
  if (ok) {
    const uint8_t* info_data = info.empty() ? &kEmptyBuffer : info.data();
    ok = psa_key_derivation_input_bytes(&operation,
                                        PSA_KEY_DERIVATION_INPUT_INFO,
                                        info_data, info.size()) == PSA_SUCCESS;
  }
  if (ok) {
    ok = psa_key_derivation_output_bytes(&operation, output.data(),
                                         output.size()) == PSA_SUCCESS;
  }

  if (operation_started &&
      psa_key_derivation_abort(&operation) != PSA_SUCCESS) {
    ok = false;
  }
  if (key_imported && psa_destroy_key(secret_key) != PSA_SUCCESS) {
    ok = false;
  }
  if (!ok) {
    secure_zero(output);
  }
  return ok;
}

bool compute_mac(etl::span<const uint8_t> key, psa_algorithm_t algorithm,
                 etl::span<const uint8_t> input, etl::span<uint8_t> output) {
  if (key.empty() || output.empty() || !initialize_crypto()) {
    secure_zero(output);
    return false;
  }

  mbedtls_svc_key_id_t key_id = 0;
  const bool key_imported =
      import_key(PSA_KEY_TYPE_HMAC, key.size() * 8U, PSA_KEY_USAGE_SIGN_MESSAGE,
                 algorithm, key, key_id);
  const uint8_t* input_data = input.empty() ? &kEmptyBuffer : input.data();
  size_t output_length = 0;
  bool ok = key_imported &&
            psa_mac_compute(key_id, algorithm, input_data, input.size(),
                            output.data(), output.size(),
                            &output_length) == PSA_SUCCESS &&
            output_length == output.size();
  if (key_imported && psa_destroy_key(key_id) != PSA_SUCCESS) {
    ok = false;
  }
  if (!ok) {
    secure_zero(output);
  }
  return ok;
}

bool verify_mac(etl::span<const uint8_t> key, psa_algorithm_t algorithm,
                etl::span<const uint8_t> input,
                etl::span<const uint8_t> expected_mac) {
  if (key.empty() || expected_mac.empty() || !initialize_crypto()) {
    return false;
  }

  mbedtls_svc_key_id_t key_id = 0;
  const bool key_imported =
      import_key(PSA_KEY_TYPE_HMAC, key.size() * 8U,
                 PSA_KEY_USAGE_VERIFY_MESSAGE, algorithm, key, key_id);
  const uint8_t* input_data = input.empty() ? &kEmptyBuffer : input.data();
  bool ok = key_imported && psa_mac_verify(key_id, algorithm, input_data,
                                           input.size(), expected_mac.data(),
                                           expected_mac.size()) == PSA_SUCCESS;
  if (key_imported && psa_destroy_key(key_id) != PSA_SUCCESS) {
    ok = false;
  }
  return ok;
}

bool derive_handshake_auth_key(etl::span<const uint8_t> secret,
                               etl::span<const uint8_t> nonce,
                               etl::span<const uint8_t> info,
                               etl::span<uint8_t> auth_key) {
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH) {
    secure_zero(auth_key);
    return false;
  }
  const auto salt = copy_progmem(RPC_HANDSHAKE_HKDF_SALT);
  return derive_hkdf_sha256(secret, salt, info, auth_key);
}

bool derive_handshake_tag(etl::span<const uint8_t> secret,
                          etl::span<const uint8_t> nonce,
                          etl::span<const uint8_t> info,
                          etl::span<uint8_t> out_tag) {
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH ||
      out_tag.size() < RPC_HANDSHAKE_TAG_LENGTH) {
    secure_zero(out_tag);
    return false;
  }

  secure_zero(out_tag);
  etl::array<uint8_t, RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> auth_key = {};
  bool ok = derive_handshake_auth_key(secret, nonce, info, auth_key);
  if (ok) {
    ok = compute_mac(
        auth_key, kHandshakeMac, nonce,
        etl::span<uint8_t>(out_tag.data(), RPC_HANDSHAKE_TAG_LENGTH));
  }
  if (!ok) {
    secure_zero(out_tag);
  }
  secure_zero(etl::span<uint8_t>(auth_key));
  return ok;
}

}  // namespace

void secure_zero(etl::span<uint8_t> buf) {
  if (!buf.empty()) {
    mbedtls_platform_zeroize(buf.data(), buf.size());
  }
}

bool derive_session_keys(etl::span<const uint8_t> secret,
                         etl::span<const uint8_t> nonce,
                         etl::span<uint8_t> host_to_mcu_key,
                         etl::span<uint8_t> mcu_to_host_key) {
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH ||
      host_to_mcu_key.size() != RPC_AEAD_KEY_SIZE ||
      mcu_to_host_key.size() != RPC_AEAD_KEY_SIZE) {
    secure_zero(host_to_mcu_key);
    secure_zero(mcu_to_host_key);
    return false;
  }

  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_SESSION);
  etl::array<uint8_t, RPC_AEAD_KEY_SIZE * 2U> keys = {};
  const bool ok =
      derive_hkdf_sha256(secret, nonce, info, etl::span<uint8_t>(keys));
  if (ok) {
    etl::copy_n(keys.begin(), host_to_mcu_key.size(), host_to_mcu_key.begin());
    etl::copy_n(keys.begin() + host_to_mcu_key.size(), mcu_to_host_key.size(),
                mcu_to_host_key.begin());
  } else {
    secure_zero(host_to_mcu_key);
    secure_zero(mcu_to_host_key);
  }
  secure_zero(etl::span<uint8_t>(keys));
  return ok;
}

bool aead_encrypt(etl::span<const uint8_t> key, etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> aad,
                  etl::span<const uint8_t> plaintext,
                  etl::span<uint8_t> ciphertext, etl::span<uint8_t> tag) {
  if (key.size() != RPC_AEAD_KEY_SIZE || nonce.size() != RPC_AEAD_NONCE_SIZE ||
      ciphertext.size() < plaintext.size() || tag.size() != RPC_AEAD_TAG_SIZE) {
    secure_zero(ciphertext);
    secure_zero(tag);
    return false;
  }

#if defined(ARDUINO_ARCH_ESP32)
  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> output = {};
  if (plaintext.size() > output.size() - RPC_AEAD_TAG_SIZE) {
    secure_zero(ciphertext);
    secure_zero(tag);
    return false;
  }

  const bool crypto_initialized = initialize_crypto();
  mbedtls_svc_key_id_t key_id = 0;
  const bool key_imported =
      crypto_initialized &&
      import_key(PSA_KEY_TYPE_CHACHA20, key.size() * 8U, PSA_KEY_USAGE_ENCRYPT,
                 kChaCha20Poly1305, key, key_id);
  const uint8_t* aad_data = aad.empty() ? &kEmptyBuffer : aad.data();
  const uint8_t* plaintext_data =
      plaintext.empty() ? &kEmptyBuffer : plaintext.data();
  size_t output_length = 0;
  bool ok = key_imported &&
            psa_aead_encrypt(key_id, kChaCha20Poly1305, nonce.data(),
                             nonce.size(), aad_data, aad.size(), plaintext_data,
                             plaintext.size(), output.data(), output.size(),
                             &output_length) == PSA_SUCCESS &&
            output_length == plaintext.size() + tag.size();
  if (ok) {
    etl::copy_n(output.begin(), plaintext.size(), ciphertext.begin());
    etl::copy_n(output.begin() + plaintext.size(), tag.size(), tag.begin());
  }
  if (key_imported && psa_destroy_key(key_id) != PSA_SUCCESS) {
    ok = false;
  }
  secure_zero(etl::span<uint8_t>(output));
#else
  const bool crypto_initialized = initialize_crypto();
  mbedtls_svc_key_id_t key_id = 0;
  const bool key_imported =
      crypto_initialized &&
      import_key(PSA_KEY_TYPE_CHACHA20, key.size() * 8U, PSA_KEY_USAGE_ENCRYPT,
                 kChaCha20Poly1305, key, key_id);
  psa_aead_operation_t operation = PSA_AEAD_OPERATION_INIT;
  bool ok = key_imported &&
            psa_aead_encrypt_setup(&operation, key_id, kChaCha20Poly1305) ==
                PSA_SUCCESS;
  if (ok) {
    ok = psa_aead_set_nonce(&operation, nonce.data(), nonce.size()) ==
         PSA_SUCCESS;
  }
  if (ok && !aad.empty()) {
    ok = psa_aead_update_ad(&operation, aad.data(), aad.size()) == PSA_SUCCESS;
  }

  size_t update_length = 0;
  if (ok && !plaintext.empty()) {
    uint8_t* output_data =
        ciphertext.empty() ? &kEmptyBuffer : ciphertext.data();
    ok = psa_aead_update(&operation, plaintext.data(), plaintext.size(),
                         output_data, ciphertext.size(),
                         &update_length) == PSA_SUCCESS;
  }

  size_t finish_length = 0;
  size_t tag_length = 0;
  if (ok && update_length <= ciphertext.size()) {
    const size_t finish_capacity = ciphertext.size() - update_length;
    uint8_t* finish_data = finish_capacity == 0U
                               ? &kEmptyBuffer
                               : ciphertext.data() + update_length;
    ok = psa_aead_finish(&operation, finish_data, finish_capacity,
                         &finish_length, tag.data(), tag.size(),
                         &tag_length) == PSA_SUCCESS;
  } else {
    ok = false;
  }
  if (ok) {
    ok = finish_length <= ciphertext.size() - update_length &&
         update_length + finish_length == plaintext.size() &&
         tag_length == tag.size();
  }

  if (crypto_initialized && psa_aead_abort(&operation) != PSA_SUCCESS) {
    ok = false;
  }
  if (key_imported && psa_destroy_key(key_id) != PSA_SUCCESS) {
    ok = false;
  }
#endif
  if (!ok) {
    secure_zero(ciphertext);
    secure_zero(tag);
  }
  return ok;
}

bool aead_decrypt(etl::span<const uint8_t> key, etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> aad,
                  etl::span<const uint8_t> ciphertext,
                  etl::span<const uint8_t> tag, etl::span<uint8_t> plaintext) {
  if (key.size() != RPC_AEAD_KEY_SIZE || nonce.size() != RPC_AEAD_NONCE_SIZE ||
      ciphertext.size() > plaintext.size() || tag.size() != RPC_AEAD_TAG_SIZE) {
    secure_zero(plaintext);
    return false;
  }

#if defined(ARDUINO_ARCH_ESP32)
  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> input = {};
  if (ciphertext.size() > input.size() - tag.size()) {
    secure_zero(plaintext);
    return false;
  }
  etl::copy(ciphertext.begin(), ciphertext.end(), input.begin());
  etl::copy(tag.begin(), tag.end(), input.begin() + ciphertext.size());

  const bool crypto_initialized = initialize_crypto();
  mbedtls_svc_key_id_t key_id = 0;
  const bool key_imported =
      crypto_initialized &&
      import_key(PSA_KEY_TYPE_CHACHA20, key.size() * 8U, PSA_KEY_USAGE_DECRYPT,
                 kChaCha20Poly1305, key, key_id);
  const uint8_t* aad_data = aad.empty() ? &kEmptyBuffer : aad.data();
  uint8_t* plaintext_data =
      plaintext.empty() ? &kEmptyBuffer : plaintext.data();
  size_t output_length = 0;
  bool ok = key_imported &&
            psa_aead_decrypt(key_id, kChaCha20Poly1305, nonce.data(),
                             nonce.size(), aad_data, aad.size(), input.data(),
                             ciphertext.size() + tag.size(), plaintext_data,
                             plaintext.size(), &output_length) == PSA_SUCCESS &&
            output_length == ciphertext.size();
  if (key_imported && psa_destroy_key(key_id) != PSA_SUCCESS) {
    ok = false;
  }
  secure_zero(etl::span<uint8_t>(input));
#else
  const bool crypto_initialized = initialize_crypto();
  mbedtls_svc_key_id_t key_id = 0;
  const bool key_imported =
      crypto_initialized &&
      import_key(PSA_KEY_TYPE_CHACHA20, key.size() * 8U, PSA_KEY_USAGE_DECRYPT,
                 kChaCha20Poly1305, key, key_id);
  psa_aead_operation_t operation = PSA_AEAD_OPERATION_INIT;
  bool ok = key_imported &&
            psa_aead_decrypt_setup(&operation, key_id, kChaCha20Poly1305) ==
                PSA_SUCCESS;
  if (ok) {
    ok = psa_aead_set_nonce(&operation, nonce.data(), nonce.size()) ==
         PSA_SUCCESS;
  }
  if (ok && !aad.empty()) {
    ok = psa_aead_update_ad(&operation, aad.data(), aad.size()) == PSA_SUCCESS;
  }

  size_t update_length = 0;
  if (ok && !ciphertext.empty()) {
    uint8_t* output_data = plaintext.empty() ? &kEmptyBuffer : plaintext.data();
    ok = psa_aead_update(&operation, ciphertext.data(), ciphertext.size(),
                         output_data, plaintext.size(),
                         &update_length) == PSA_SUCCESS;
  }

  size_t verify_length = 0;
  if (ok && update_length <= plaintext.size()) {
    const size_t verify_capacity = plaintext.size() - update_length;
    uint8_t* verify_data = verify_capacity == 0U
                               ? &kEmptyBuffer
                               : plaintext.data() + update_length;
    ok = psa_aead_verify(&operation, verify_data, verify_capacity,
                         &verify_length, tag.data(), tag.size()) == PSA_SUCCESS;
  } else {
    ok = false;
  }
  if (ok) {
    ok = verify_length <= plaintext.size() - update_length &&
         update_length + verify_length == ciphertext.size();
  }

  if (crypto_initialized && psa_aead_abort(&operation) != PSA_SUCCESS) {
    ok = false;
  }
  if (key_imported && psa_destroy_key(key_id) != PSA_SUCCESS) {
    ok = false;
  }
#endif
  if (!ok) {
    secure_zero(plaintext);
  }
  return ok;
}

bool handshake_generate_tag(etl::span<const uint8_t> secret,
                            etl::span<const uint8_t> nonce,
                            etl::span<uint8_t> out_tag) {
  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_AUTH);
  return derive_handshake_tag(secret, nonce, info, out_tag);
}

bool handshake_generate_response_tag(etl::span<const uint8_t> secret,
                                     etl::span<const uint8_t> nonce,
                                     etl::span<uint8_t> out_tag) {
  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_RESPONSE);
  return derive_handshake_tag(secret, nonce, info, out_tag);
}

bool handshake_verify_tag(etl::span<const uint8_t> secret,
                          etl::span<const uint8_t> nonce,
                          etl::span<const uint8_t> received_tag) {
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH ||
      received_tag.size() != RPC_HANDSHAKE_TAG_LENGTH) {
    return false;
  }

  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_AUTH);
  etl::array<uint8_t, RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> auth_key = {};
  bool ok = derive_handshake_auth_key(secret, nonce, info, auth_key);
  if (ok) {
    ok = verify_mac(auth_key, kHandshakeMac, nonce, received_tag);
  }
  secure_zero(etl::span<uint8_t>(auth_key));
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
  const auto sha_msg = copy_progmem(kat_sha256_msg);
  const auto sha_expected = copy_progmem(kat_sha256_expected);
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> digest = {};
  size_t digest_length = 0;
  const bool sha_ok =
      initialize_crypto() &&
      psa_hash_compute(PSA_ALG_SHA_256, sha_msg.data(), sha_msg.size(),
                       digest.data(), digest.size(),
                       &digest_length) == PSA_SUCCESS &&
      digest_length == digest.size() &&
      etl::equal(digest.begin(), digest.end(), sha_expected.begin());

  const auto hmac_key = copy_progmem(kat_hmac_key);
  const auto hmac_data = copy_progmem(kat_hmac_data);
  const auto hmac_expected = copy_progmem(kat_hmac_expected);
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> hmac_result = {};
  const bool hmac_ok =
      compute_mac(hmac_key, kHmacSha256, hmac_data, hmac_result) &&
      etl::equal(hmac_result.begin(), hmac_result.end(), hmac_expected.begin());

  const auto handshake_secret = copy_progmem(kat_handshake_secret);
  const auto handshake_nonce = copy_progmem(kat_handshake_nonce);
  const auto handshake_request_expected =
      copy_progmem(kat_handshake_request_tag);
  const auto handshake_response_expected =
      copy_progmem(kat_handshake_response_tag);
  etl::array<uint8_t, RPC_HANDSHAKE_TAG_LENGTH> handshake_request = {};
  etl::array<uint8_t, RPC_HANDSHAKE_TAG_LENGTH> handshake_response = {};
  const bool handshake_request_ok =
      handshake_generate_tag(handshake_secret, handshake_nonce,
                             handshake_request) &&
      etl::equal(handshake_request.begin(), handshake_request.end(),
                 handshake_request_expected.begin()) &&
      handshake_verify_tag(handshake_secret, handshake_nonce,
                           handshake_request_expected);
  const bool handshake_response_ok =
      handshake_generate_response_tag(handshake_secret, handshake_nonce,
                                      handshake_response) &&
      etl::equal(handshake_response.begin(), handshake_response.end(),
                 handshake_response_expected.begin());

  const auto aead_key = copy_progmem(kat_aead_key);
  const auto aead_nonce = copy_progmem(kat_aead_nonce);
  const auto aead_aad = copy_progmem(kat_aead_aad);
  const auto aead_plaintext = copy_progmem(kat_aead_plaintext);
  const auto aead_expected = copy_progmem(kat_aead_ciphertext);
  const auto aead_expected_tag = copy_progmem(kat_aead_tag);
  etl::array<uint8_t, 4> ciphertext = {};
  etl::array<uint8_t, RPC_AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, 4> decrypted = {};
  const bool aead_encrypted = aead_encrypt(aead_key, aead_nonce, aead_aad,
                                           aead_plaintext, ciphertext, tag);
  const bool aead_encrypt_ok =
      aead_encrypted &&
      etl::equal(ciphertext.begin(), ciphertext.end(), aead_expected.begin()) &&
      etl::equal(tag.begin(), tag.end(), aead_expected_tag.begin());
  const bool aead_decrypt_ok =
      aead_decrypt(aead_key, aead_nonce, aead_aad, aead_expected,
                   aead_expected_tag, decrypted) &&
      etl::equal(decrypted.begin(), decrypted.end(), aead_plaintext.begin());

  auto invalid_tag = aead_expected_tag;
  invalid_tag[0] ^= 0x01U;
  etl::array<uint8_t, 4> rejected_plaintext = {0xAAU, 0xAAU, 0xAAU, 0xAAU};
  const bool bad_tag_rejected =
      !aead_decrypt(aead_key, aead_nonce, aead_aad, aead_expected, invalid_tag,
                    rejected_plaintext);
  const etl::array<uint8_t, 4> zero_plaintext = {};
  const bool rejected_plaintext_cleared =
      etl::equal(rejected_plaintext.begin(), rejected_plaintext.end(),
                 zero_plaintext.begin());

  secure_zero(etl::span<uint8_t>(digest));
  secure_zero(etl::span<uint8_t>(hmac_result));
  secure_zero(etl::span<uint8_t>(handshake_request));
  secure_zero(etl::span<uint8_t>(handshake_response));
  secure_zero(etl::span<uint8_t>(ciphertext));
  secure_zero(etl::span<uint8_t>(tag));
  secure_zero(etl::span<uint8_t>(decrypted));
  secure_zero(etl::span<uint8_t>(rejected_plaintext));
  return sha_ok && hmac_ok && handshake_request_ok && handshake_response_ok &&
         aead_encrypt_ok && aead_decrypt_ok && bad_tag_rejected &&
         rejected_plaintext_cleared;
}

#endif  // BRIDGE_ENABLE_POST_TESTS

}  // namespace security
}  // namespace rpc
