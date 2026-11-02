/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include "security.h"

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/string_view.h>
#include <mbedtls/chachapoly.h>
#include <mbedtls/constant_time.h>
#include <mbedtls/hkdf.h>
#include <mbedtls/md.h>
#include <mbedtls/platform_util.h>
#include <mbedtls/sha256.h>

#include "../config/bridge_config.h"

namespace rpc {
namespace security {
namespace {

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
  const auto salt = copy_progmem(RPC_HANDSHAKE_HKDF_SALT);
  const mbedtls_md_info_t* md_info =
      mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  etl::array<uint8_t, RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> auth_key = {};
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> full_tag = {};
  bool ok = md_info != nullptr;
  if (ok) {
    ok = mbedtls_hkdf(md_info, salt.data(), salt.size(), secret.data(),
                      secret.size(), info.data(), info.size(), auth_key.data(),
                      auth_key.size()) == 0;
  }
  if (ok) {
    ok = mbedtls_md_hmac(md_info, auth_key.data(), auth_key.size(),
                         nonce.data(), nonce.size(), full_tag.data()) == 0;
  }
  if (ok) {
    etl::copy_n(full_tag.begin(), RPC_HANDSHAKE_TAG_LENGTH, out_tag.begin());
  } else {
    secure_zero(out_tag);
  }
  secure_zero(etl::span<uint8_t>(auth_key));
  secure_zero(etl::span<uint8_t>(full_tag));
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
  const mbedtls_md_info_t* md_info =
      mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  etl::array<uint8_t, RPC_AEAD_KEY_SIZE * 2U> keys = {};
  const bool ok = md_info != nullptr &&
                  mbedtls_hkdf(md_info, nonce.data(), nonce.size(),
                               secret.data(), secret.size(), info.data(),
                               info.size(), keys.data(), keys.size()) == 0;
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

  static uint8_t kEmpty = 0U;
  mbedtls_chachapoly_context ctx;
  mbedtls_chachapoly_init(&ctx);
  bool ok = mbedtls_chachapoly_setkey(&ctx, key.data()) == 0;
  if (ok) {
    ok = mbedtls_chachapoly_encrypt_and_tag(
             &ctx, plaintext.size(), nonce.data(),
             aad.empty() ? &kEmpty : aad.data(), aad.size(),
             plaintext.empty() ? &kEmpty : plaintext.data(),
             ciphertext.empty() ? &kEmpty : ciphertext.data(), tag.data()) == 0;
  }
  mbedtls_chachapoly_free(&ctx);
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

  static uint8_t kEmpty = 0U;
  mbedtls_chachapoly_context ctx;
  mbedtls_chachapoly_init(&ctx);
  bool ok = mbedtls_chachapoly_setkey(&ctx, key.data()) == 0;
  if (ok) {
    ok = mbedtls_chachapoly_auth_decrypt(
             &ctx, ciphertext.size(), nonce.data(),
             aad.empty() ? &kEmpty : aad.data(), aad.size(), tag.data(),
             ciphertext.empty() ? &kEmpty : ciphertext.data(),
             plaintext.empty() ? &kEmpty : plaintext.data()) == 0;
  }
  mbedtls_chachapoly_free(&ctx);
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
  if (received_tag.size() != RPC_HANDSHAKE_TAG_LENGTH) {
    return false;
  }

  etl::array<uint8_t, RPC_HANDSHAKE_TAG_LENGTH> expected_tag = {};
  const bool generated = handshake_generate_tag(secret, nonce, expected_tag);
  const bool tag_ok =
      generated && mbedtls_ct_memcmp(expected_tag.data(), received_tag.data(),
                                     expected_tag.size()) == 0;
  secure_zero(etl::span<uint8_t>(expected_tag));
  return tag_ok;
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
  const bool sha_ok =
      mbedtls_sha256(sha_msg.data(), sha_msg.size(), digest.data(), 0) == 0 &&
      etl::equal(digest.begin(), digest.end(), sha_expected.begin());

  const auto hmac_key = copy_progmem(kat_hmac_key);
  const auto hmac_data = copy_progmem(kat_hmac_data);
  const auto hmac_expected = copy_progmem(kat_hmac_expected);
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> hmac_result = {};
  const mbedtls_md_info_t* md_info =
      mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  const bool hmac_ok =
      md_info != nullptr &&
      mbedtls_md_hmac(md_info, hmac_key.data(), hmac_key.size(),
                      hmac_data.data(), hmac_data.size(),
                      hmac_result.data()) == 0 &&
      etl::equal(hmac_result.begin(), hmac_result.end(), hmac_expected.begin());

  const auto aead_key = copy_progmem(kat_aead_key);
  const auto aead_nonce = copy_progmem(kat_aead_nonce);
  const auto aead_aad = copy_progmem(kat_aead_aad);
  const auto aead_plaintext = copy_progmem(kat_aead_plaintext);
  const auto aead_expected = copy_progmem(kat_aead_ciphertext);
  const auto aead_expected_tag = copy_progmem(kat_aead_tag);
  etl::array<uint8_t, 4> ciphertext = {};
  etl::array<uint8_t, RPC_AEAD_TAG_SIZE> tag = {};
  const bool aead_encrypted = aead_encrypt(aead_key, aead_nonce, aead_aad,
                                           aead_plaintext, ciphertext, tag);
  const bool aead_ok =
      aead_encrypted &&
      etl::equal(ciphertext.begin(), ciphertext.end(), aead_expected.begin()) &&
      etl::equal(tag.begin(), tag.end(), aead_expected_tag.begin());

  secure_zero(etl::span<uint8_t>(digest));
  secure_zero(etl::span<uint8_t>(hmac_result));
  secure_zero(etl::span<uint8_t>(ciphertext));
  secure_zero(etl::span<uint8_t>(tag));
  return sha_ok && hmac_ok && aead_ok;
}

#endif  // BRIDGE_ENABLE_POST_TESTS

}  // namespace security
}  // namespace rpc
