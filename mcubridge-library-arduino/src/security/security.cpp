/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include "security.h"

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/numeric.h>
#include <etl/string_view.h>

#include "../config/bridge_config.h"  // IWYU pragma: keep
#include "pw_bytes/span.h"
#include "pw_crypto/sha256.h"
#include "pw_span/span.h"

namespace rpc {
namespace security {

static bool constant_time_equal(const uint8_t* a, const uint8_t* b,
                                size_t len) {
  return etl::accumulate(a, a + len, uint8_t(0),
                         [&b](uint8_t acc, uint8_t val) {
                           return static_cast<uint8_t>(acc | (val ^ *b++));
                         }) == 0;
}

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

static bool hmac_sha256(etl::span<const uint8_t> key,
                        etl::span<const uint8_t> data,
                        etl::span<const uint8_t> extra,
                        etl::span<uint8_t> out_mac) {
  constexpr size_t kBlockSize = 64U;
  constexpr size_t kDigestSize = pw::crypto::sha256::kDigestSizeBytes;
  if (key.size() > kBlockSize || out_mac.size() < kDigestSize) {
    secure_zero(out_mac);
    return false;
  }

  etl::array<uint8_t, kBlockSize> k_pad = {};
  etl::copy(key.begin(), key.end(), k_pad.begin());

  etl::array<uint8_t, kBlockSize> i_key_pad = {};
  etl::array<uint8_t, kBlockSize> o_key_pad = {};
  for (size_t i = 0; i < kBlockSize; ++i) {
    i_key_pad[i] = static_cast<uint8_t>(k_pad[i] ^ 0x36U);
    o_key_pad[i] = static_cast<uint8_t>(k_pad[i] ^ 0x5CU);
  }

  etl::array<uint8_t, pw::crypto::sha256::kDigestSizeBytes> inner_hash = {};
  pw::crypto::sha256::Sha256 inner;
  inner.Update(pw::as_bytes(pw::span(i_key_pad.data(), i_key_pad.size())));
  inner.Update(pw::as_bytes(pw::span(data.data(), data.size())));
  if (!extra.empty()) {
    inner.Update(pw::as_bytes(pw::span(extra.data(), extra.size())));
  }
  bool ok = inner
                .Final(pw::as_writable_bytes(
                    pw::span(inner_hash.data(), inner_hash.size())))
                .ok();

  if (ok) {
    pw::crypto::sha256::Sha256 outer;
    outer.Update(pw::as_bytes(pw::span(o_key_pad.data(), o_key_pad.size())));
    outer.Update(pw::as_bytes(pw::span(inner_hash.data(), inner_hash.size())));
    ok = outer
             .Final(pw::as_writable_bytes(
                 pw::span(out_mac.data(), out_mac.size())))
             .ok();
  }

  secure_zero(etl::span<uint8_t>(k_pad.data(), k_pad.size()));
  secure_zero(etl::span<uint8_t>(i_key_pad.data(), i_key_pad.size()));
  secure_zero(etl::span<uint8_t>(o_key_pad.data(), o_key_pad.size()));
  secure_zero(etl::span<uint8_t>(inner_hash.data(), inner_hash.size()));
  if (!ok) {
    secure_zero(out_mac);
  }
  return ok;
}

// --- HKDF & HMAC Implementation (built on pw::crypto::sha256) ---

static bool derive_handshake_tag(etl::span<const uint8_t> secret,
                                 etl::span<const uint8_t> nonce,
                                 etl::span<const uint8_t> info,
                                 etl::span<uint8_t> out_tag) {
  if (secret.empty() || nonce.size() != rpc::RPC_HANDSHAKE_NONCE_LENGTH ||
      out_tag.size() < rpc::RPC_HANDSHAKE_TAG_LENGTH) {
    etl::fill(out_tag.begin(), out_tag.end(), 0U);
    return false;
  }

  etl::fill(out_tag.begin(), out_tag.end(), 0U);
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> prk = {};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> handshake_key = {};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> hmac_tag = {};
  const auto salt = copy_progmem(rpc::RPC_HANDSHAKE_HKDF_SALT);
  constexpr etl::array<uint8_t, 1U> kHkdfCounter = {0x01U};

  // RFC 5869 HKDF-Extract(salt, IKM=secret) -> PRK
  bool ok = hmac_sha256(salt, secret, etl::span<const uint8_t>(), prk);
  // RFC 5869 HKDF-Expand(PRK, info || 0x01, L=32) -> handshake_key
  if (ok) {
    ok = hmac_sha256(prk, info, kHkdfCounter, handshake_key);
  }
  // HMAC-SHA256(handshake_key, nonce) -> hmac_tag
  if (ok) {
    ok =
        hmac_sha256(handshake_key, nonce, etl::span<const uint8_t>(), hmac_tag);
  }

  if (ok) {
    etl::copy_n(hmac_tag.begin(), rpc::RPC_HANDSHAKE_TAG_LENGTH,
                out_tag.begin());
  } else {
    etl::fill(out_tag.begin(), out_tag.end(), 0U);
  }

  secure_zero(etl::span<uint8_t>(prk.data(), prk.size()));
  secure_zero(etl::span<uint8_t>(handshake_key.data(), handshake_key.size()));
  secure_zero(etl::span<uint8_t>(hmac_tag.data(), hmac_tag.size()));
  return ok;
}

bool handshake_generate_tag(etl::span<const uint8_t> secret,
                            etl::span<const uint8_t> nonce,
                            etl::span<uint8_t> out_tag) {
  const auto info = copy_progmem(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH);
  return derive_handshake_tag(secret, nonce, info, out_tag);
}

bool handshake_generate_response_tag(etl::span<const uint8_t> secret,
                                     etl::span<const uint8_t> nonce,
                                     etl::span<uint8_t> out_tag) {
  const auto info = copy_progmem(rpc::RPC_HANDSHAKE_HKDF_INFO_RESPONSE);
  return derive_handshake_tag(secret, nonce, info, out_tag);
}

bool handshake_verify_tag(etl::span<const uint8_t> secret,
                          etl::span<const uint8_t> nonce,
                          etl::span<const uint8_t> received_tag) {
  if (received_tag.size() != rpc::RPC_HANDSHAKE_TAG_LENGTH) {
    return false;
  }

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> expected_tag = {};
  const bool generated = handshake_generate_tag(secret, nonce, expected_tag);
  const bool tag_ok =
      generated && constant_time_equal(expected_tag.data(),
                                       received_tag.data(), expected_tag.size());
  secure_zero(etl::span<uint8_t>(expected_tag));
  return tag_ok;
}

// --- Self-Tests Implementation ---

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

bool run_cryptographic_self_tests() {
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> actual = {};

  // 1. SHA256 KAT (pw::crypto::sha256)
  const auto sha_msg = copy_progmem(kat_sha256_msg);
  const bool sha_res_ok =
      pw::crypto::sha256::Hash(
          pw::as_bytes(pw::span(sha_msg.data(), sha_msg.size())),
          pw::as_writable_bytes(pw::span(actual.data(), actual.size())))
          .ok();
  const auto sha_expected = copy_progmem(kat_sha256_expected);
  const bool ok = sha_res_ok && etl::equal(actual.begin(), actual.end(),
                                           sha_expected.begin());

  // 2. HMAC-SHA256 KAT (pw::crypto::sha256)
  const auto hmac_key = copy_progmem(kat_hmac_key);
  const auto hmac_data = copy_progmem(kat_hmac_data);
  const bool hmac_res_ok =
      hmac_sha256(hmac_key, hmac_data, etl::span<const uint8_t>(), actual);
  const auto hmac_expected = copy_progmem(kat_hmac_expected);
  const bool hmac_ok = hmac_res_ok && etl::equal(actual.begin(), actual.end(),
                                                 hmac_expected.begin());

  const uint8_t val_ok = static_cast<uint8_t>(ok);
  const uint8_t val_hmac = static_cast<uint8_t>(hmac_ok);
  return (val_ok & val_hmac) != 0U;
}

#endif  // BRIDGE_ENABLE_POST_TESTS

}  // namespace security
}  // namespace rpc
