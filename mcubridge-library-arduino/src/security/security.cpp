/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include "security.h"

#include <Arduino.h>

// Arduino.h defines function-like min/max macros. Remove them before ETL
// headers are parsed; undefining them after ETL has already been included is
// too late and breaks numeric_limits<T>::min()/max() declarations.
#if defined(min)
#undef min
#endif
#if defined(max)
#undef max
#endif

#include <etl/algorithm.h>
#include <etl/array.h>
#include <pw_span/span.h>

#include <wolfssl/wolfcrypt/chacha20_poly1305.h>
#include <wolfssl/wolfcrypt/hmac.h>
#include <wolfssl/wolfcrypt/kdf.h>
#include <wolfssl/wolfcrypt/misc.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/sha256.h>

#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"

namespace rpc {
namespace security {
namespace {

static uint8_t kEmptyBuffer = 0U;

static_assert(RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH == RPC_SHA256_DIGEST_SIZE,
              "Handshake HMAC scratch must hold one SHA-256 digest");
static_assert(RPC_HANDSHAKE_TAG_LENGTH <= RPC_SHA256_DIGEST_SIZE,
              "Handshake tag cannot exceed the HMAC digest");
static_assert(RPC_AEAD_KEY_SIZE * 2U <= 255U * RPC_SHA256_DIGEST_SIZE,
              "Session key output exceeds the HKDF-SHA256 limit");
#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR) || \
    defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_ESP32)
static_assert(sizeof(size_t) <= sizeof(word32),
              "Embedded span sizes must fit WolfCrypt word32 lengths");
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

bool derive_hkdf_sha256(pw::span<const uint8_t> secret,
                        pw::span<const uint8_t> salt,
                        pw::span<const uint8_t> info,
                        pw::span<uint8_t> output) {
  // All callers provide a non-empty secret and fixed, statically bounded
  // output buffers (32-byte handshake key or 64-byte RX||TX key pair).
  // Embedded size_t is at most word32; the static assertion above enforces it.
  const byte* salt_data = salt.empty() ? &kEmptyBuffer : salt.data();
  const byte* info_data = info.empty() ? &kEmptyBuffer : info.data();
  const int status = wc_HKDF(
      WC_SHA256, secret.data(), static_cast<word32>(secret.size()), salt_data,
      static_cast<word32>(salt.size()), info_data,
      static_cast<word32>(info.size()), output.data(),
      static_cast<word32>(output.size()));
  if (status != 0) {
    secure_zero(output);
    return false;
  }
  return true;
}

bool compute_hmac_sha256(pw::span<const uint8_t> key,
                         pw::span<const uint8_t> input,
                         pw::span<uint8_t> digest_scratch) {
  // Private call sites pass the 32-byte derived authentication key as both
  // key storage and digest scratch, and a fixed-size handshake nonce as input.
  const byte* input_data = input.empty() ? &kEmptyBuffer : input.data();
  Hmac hmac{};
  if (wc_HmacInit(&hmac, nullptr, 0) != 0) {
    secure_zero(digest_scratch);
    return false;
  }

  // wc_HmacSetKey copies the key into the HMAC context's ipad/opad. The
  // caller can therefore reuse the same 32-byte buffer for the final digest.
  // This avoids a second SHA-256-sized stack buffer on AVR.
  bool ok = wc_HmacSetKey(&hmac, WC_SHA256, key.data(),
                          static_cast<word32>(key.size())) == 0;
  if (ok) {
    ok = wc_HmacUpdate(&hmac, input_data,
                       static_cast<word32>(input.size())) == 0;
  }
#if defined(WOLF_CRYPTO_CB)
  // Crypto callbacks may retain the key pointer; keep the caller's buffer
  // unchanged until the provider has completed the final operation.
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> full_digest = {};
  if (ok) ok = wc_HmacFinal(&hmac, full_digest.data()) == 0;
  wc_HmacFree(&hmac);
  if (ok) {
    etl::copy(full_digest.begin(), full_digest.end(), digest_scratch.begin());
  } else {
    secure_zero(digest_scratch);
  }
  secure_zero(pw::span<uint8_t>(full_digest));
#else
  if (ok) ok = wc_HmacFinal(&hmac, digest_scratch.data()) == 0;
  wc_HmacFree(&hmac);
  if (!ok) secure_zero(digest_scratch);
#endif
  return ok;
}

// Combine results without short-circuiting: both cryptographic paths must
// execute regardless of the first result. Use integer operands so static
// analyzers do not mistake this constant-time intent for a boolean typo.
bool combine_ok(bool lhs, bool rhs) {
  const uint8_t lhs_value = lhs ? 1U : 0U;
  const uint8_t rhs_value = rhs ? 1U : 0U;
  return (lhs_value & rhs_value) != 0U;
}

bool verify_hmac_sha256(pw::span<uint8_t> key_and_digest,
                        pw::span<const uint8_t> input,
                        pw::span<const uint8_t> expected_mac) {
  // The public handshake verifier validates the fixed 16-byte tag length;
  // key_and_digest is always the 32-byte HKDF output buffer.
  // The HMAC provider has already copied the key into its context before it
  // overwrites this caller-owned buffer with the digest.
  const bool computed = compute_hmac_sha256(
      pw::span<const uint8_t>(key_and_digest), input, key_and_digest);
  const bool matches =
      ConstantCompare(key_and_digest.data(), expected_mac.data(),
                      static_cast<int>(expected_mac.size())) == 0;
  const bool ok = combine_ok(computed, matches);
  secure_zero(key_and_digest);
  return ok;
}

bool derive_handshake_auth_key(pw::span<const uint8_t> secret,
                               pw::span<const uint8_t> info,
                               pw::span<uint8_t> auth_key) {
  const auto salt = copy_progmem(RPC_HANDSHAKE_HKDF_SALT);
  return derive_hkdf_sha256(secret, salt, info, auth_key);
}

bool derive_handshake_tag(pw::span<const uint8_t> secret,
                          pw::span<const uint8_t> nonce,
                          pw::span<const uint8_t> info,
                          pw::span<uint8_t> out_tag) {
  const bool invalid_dimensions =
      secret.empty() | (nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH) |
      (out_tag.size() < RPC_HANDSHAKE_TAG_LENGTH);
  if (invalid_dimensions) {
    secure_zero(out_tag);
    return false;
  }

  secure_zero(out_tag);
  etl::array<uint8_t, RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> auth_key = {};
  const bool key_ok = derive_handshake_auth_key(secret, info, auth_key);
  // Always execute the HMAC path. A failed HKDF clears auth_key, and the
  // combined result remains false; this avoids a data-dependent early exit.
  const bool hmac_ok = compute_hmac_sha256(auth_key, nonce, auth_key);
  const bool tag_ok = combine_ok(key_ok, hmac_ok);
  if (tag_ok) {
    etl::copy_n(auth_key.begin(), RPC_HANDSHAKE_TAG_LENGTH, out_tag.begin());
  } else {
    secure_zero(out_tag);
  }
  secure_zero(pw::span<uint8_t>(auth_key));
  return tag_ok;
}

}  // namespace

void secure_zero(pw::span<uint8_t> buf) {
  if (!buf.empty()) {
    ForceZero(buf.data(), buf.size());
  }
}

bool derive_session_keys(pw::span<const uint8_t> secret,
                         pw::span<const uint8_t> nonce,
                         pw::span<uint8_t> session_keys) {
  constexpr size_t kSessionKeyBytes = RPC_AEAD_KEY_SIZE * 2U;
  if (secret.empty() || nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH ||
      session_keys.size() != kSessionKeyBytes) {
    secure_zero(session_keys);
    return false;
  }

  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_SESSION);
  // HKDF writes directly into the caller's contiguous RX||TX key storage.
  // This removes the previous 64-byte local array and its copy operations.
  return derive_hkdf_sha256(secret, nonce, info, session_keys);
}

bool aead_encrypt(pw::span<const uint8_t> key, pw::span<const uint8_t> nonce,
                  pw::span<const uint8_t> aad,
                  pw::span<const uint8_t> plaintext,
                  pw::span<uint8_t> ciphertext, pw::span<uint8_t> tag) {
  const bool invalid_dimensions =
      (key.size() != RPC_AEAD_KEY_SIZE) |
      (nonce.size() != RPC_AEAD_NONCE_SIZE) |
      (ciphertext.size() < plaintext.size()) |
      (tag.size() != RPC_AEAD_TAG_SIZE);
  if (invalid_dimensions) {
    secure_zero(ciphertext);
    secure_zero(tag);
    return false;
  }

  const uint8_t* aad_data = aad.empty() ? &kEmptyBuffer : aad.data();
  const uint8_t* plaintext_data =
      plaintext.empty() ? &kEmptyBuffer : plaintext.data();
  uint8_t* ciphertext_data =
      ciphertext.empty() ? &kEmptyBuffer : ciphertext.data();
  const int status = wc_ChaCha20Poly1305_Encrypt(
      key.data(), nonce.data(), aad_data, static_cast<word32>(aad.size()),
      plaintext_data, static_cast<word32>(plaintext.size()), ciphertext_data,
      tag.data());
  if (status != 0) {
    secure_zero(ciphertext);
    secure_zero(tag);
    return false;
  }
  return true;
}

bool aead_decrypt(pw::span<const uint8_t> key, pw::span<const uint8_t> nonce,
                  pw::span<const uint8_t> aad,
                  pw::span<const uint8_t> ciphertext,
                  pw::span<const uint8_t> tag, pw::span<uint8_t> plaintext) {
  const bool invalid_dimensions =
      (key.size() != RPC_AEAD_KEY_SIZE) |
      (nonce.size() != RPC_AEAD_NONCE_SIZE) |
      (ciphertext.size() > plaintext.size()) |
      (tag.size() != RPC_AEAD_TAG_SIZE);
  if (invalid_dimensions) {
    secure_zero(plaintext);
    return false;
  }

  const uint8_t* aad_data = aad.empty() ? &kEmptyBuffer : aad.data();
  const uint8_t* ciphertext_data =
      ciphertext.empty() ? &kEmptyBuffer : ciphertext.data();
  uint8_t* plaintext_data =
      plaintext.empty() ? &kEmptyBuffer : plaintext.data();
  const bool ok =
      (wc_ChaCha20Poly1305_Decrypt(
           key.data(), nonce.data(), aad_data, static_cast<word32>(aad.size()),
           ciphertext_data, static_cast<word32>(ciphertext.size()), tag.data(),
           plaintext_data) == 0);
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
  const bool invalid_dimensions =
      secret.empty() | (nonce.size() != RPC_HANDSHAKE_NONCE_LENGTH) |
      (received_tag.size() != RPC_HANDSHAKE_TAG_LENGTH);
  if (invalid_dimensions) return false;

  const auto info = copy_progmem(RPC_HANDSHAKE_HKDF_INFO_AUTH);
  etl::array<uint8_t, RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> auth_key = {};
  const bool key_ok = derive_handshake_auth_key(secret, info, auth_key);
  const bool hmac_ok =
      verify_hmac_sha256(pw::span<uint8_t>(auth_key), nonce, received_tag);
  const bool ok = combine_ok(key_ok, hmac_ok);
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
  const auto sha_msg = copy_progmem(kat_sha256_msg);
  const auto sha_expected = copy_progmem(kat_sha256_expected);
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> digest = {};
  wc_Sha256 sha;
  wc_InitSha256(&sha);
  wc_Sha256Update(&sha, sha_msg.data(), static_cast<word32>(sha_msg.size()));
  wc_Sha256Final(&sha, digest.data());
  wc_Sha256Free(&sha);
  bool ok = etl::equal(digest.begin(), digest.end(), sha_expected.begin());

  const auto hmac_key = copy_progmem(kat_hmac_key);
  const auto hmac_data = copy_progmem(kat_hmac_data);
  const auto hmac_expected = copy_progmem(kat_hmac_expected);
  etl::array<uint8_t, RPC_SHA256_DIGEST_SIZE> hmac_result = {};
  ok = combine_ok(ok, compute_hmac_sha256(hmac_key, hmac_data, hmac_result));
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
#if defined(BRIDGE_FAULT_INJECTION)
  // A KAT mismatch must prevent the Bridge from enabling the link. The
  // injected fault is test-only and does not alter production firmware.
  if (bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::KAT_SHA256_MISMATCH)) {
    ok = false;
  }
  if (bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::KAT_HMAC_MISMATCH)) {
    ok = false;
  }
#endif
  return ok;
}

#else
bool run_cryptographic_self_tests() {
  return true;
}
#endif  // BRIDGE_ENABLE_POST_TESTS

}  // namespace security
}  // namespace rpc
