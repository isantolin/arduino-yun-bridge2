/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include "security.h"

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/string_view.h>
#include <wolfssl/wolfcrypt/chacha20_poly1305.h>
#include <wolfssl/wolfcrypt/hash.h>
#include <wolfssl/wolfcrypt/hmac.h>
#include <wolfssl/wolfcrypt/kdf.h>

#include "../config/bridge_config.h"  // IWYU pragma: keep
#include "../protocol/rpc_structs.h"
#include "pb_encode.h"

namespace rpc {
namespace security {

static bool constant_time_equal(const uint8_t* a, const uint8_t* b,
                                size_t len) {
  return etl::accumulate(a, a + len, uint8_t(0),
                         [&b](uint8_t acc, uint8_t val) {
                           return static_cast<uint8_t>(acc | (val ^ *b++));
                         }) == 0;
}

// --- HKDF Implementation ---

bool handshake_authenticate(etl::span<const uint8_t> secret,
                            etl::span<const uint8_t> nonce,
                            etl::span<const uint8_t> received_tag,
                            etl::span<uint8_t> out_tag) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> handshake_key = {};
  wc_HKDF(WC_SHA256, secret.data(), static_cast<word32>(secret.size()),
          rpc::RPC_HANDSHAKE_HKDF_SALT.data(),
          static_cast<word32>(rpc::RPC_HANDSHAKE_HKDF_SALT.size()),
          rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH.data(),
          static_cast<word32>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH.size()),
          handshake_key.data(), static_cast<word32>(handshake_key.size()));

  Hmac hmac_engine;
  wc_HmacSetKey(&hmac_engine, WC_SHA256, handshake_key.data(),
                rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH);
  wc_HmacUpdate(&hmac_engine, nonce.data(), static_cast<word32>(nonce.size()));
  wc_HmacFinal(&hmac_engine, out_tag.data());

  bool tag_ok = true;
  if (!received_tag.empty()) {
    if (received_tag.size() != rpc::RPC_HANDSHAKE_TAG_LENGTH) {
      tag_ok = false;
    } else {
      tag_ok = constant_time_equal(out_tag.data(), received_tag.data(),
                                   rpc::RPC_HANDSHAKE_TAG_LENGTH);
    }
  }
  secure_zero(etl::span<uint8_t>(handshake_key.data(), handshake_key.size()));
  return tag_ok;
}

void derive_session_key(etl::span<const uint8_t> secret,
                        etl::span<const uint8_t> nonce,
                        etl::span<uint8_t> out_key) {
  wc_HKDF(WC_SHA256, secret.data(), static_cast<word32>(secret.size()),
          nonce.data(), static_cast<word32>(nonce.size()),
          rpc::RPC_HANDSHAKE_HKDF_INFO_SESSION.data(),
          static_cast<word32>(rpc::RPC_HANDSHAKE_HKDF_INFO_SESSION.size()),
          out_key.data(), static_cast<word32>(out_key.size()));
}

static size_t build_aad(uint16_t cmd_id, uint16_t seq_id,
                        etl::span<uint8_t> out_ad) {
  payload::RpcEnvelope aad_env = rpc_pb_RpcEnvelope_init_zero;
  aad_env.version = rpc::PROTOCOL_VERSION;
  aad_env.command_id = cmd_id;
  aad_env.sequence_id = seq_id;

  etl::fill(out_ad.begin(), out_ad.end(), 0U);
  pb_ostream_t stream = pb_ostream_from_buffer(out_ad.data(), out_ad.size());
  (void)pb_encode(&stream, rpc::Payload::get_fields<rpc_pb_RpcEnvelope>(),
                  &aad_env);
  return stream.bytes_written;
}

bool aead_encrypt_frame(uint16_t cmd_id, uint16_t seq_id,
                        etl::span<const uint8_t> in,
                        etl::span<const uint8_t> key, uint64_t* nonce_counter,
                        etl::span<uint8_t> out_payload,
                        etl::span<uint8_t> out_nonce,
                        etl::span<uint8_t> out_tag) {
  if (nonce_counter) (*nonce_counter)++;
  const uint64_t current_nonce = nonce_counter ? *nonce_counter : 0;

  etl::fill(out_nonce.begin(), out_nonce.end(), 0U);
  // [SIL-2/H-4] Compile-time verification of the nonce layout:
  // bytes [0..2] = "MCU" prefix (3 bytes)
  // byte  [3]   = 0x00 padding (zeroed by fill above)
  // bytes [4..11] = 64-bit counter big-endian (8 bytes)
  // Total = 12 bytes == AEAD_NONCE_SIZE.
  static_assert(3U + 1U + sizeof(uint64_t) == rpc::RPC_AEAD_NONCE_SIZE,
                "[SIL-2] Nonce layout mismatch: prefix(3) + pad(1) + "
                "counter(8) must equal RPC_AEAD_NONCE_SIZE");
  constexpr etl::string_view mcu_prefix("MCU");
  etl::copy_n(mcu_prefix.begin(), 3, out_nonce.begin());
  etl::byte_stream_writer n_writer(out_nonce.subspan(4), etl::endian::big);
  n_writer.write<uint64_t>(current_nonce);

  etl::array<uint8_t, 32> ad;
  const size_t ad_len = build_aad(cmd_id, seq_id, etl::span<uint8_t>(ad));

  return wc_ChaCha20Poly1305_Encrypt(
             const_cast<byte*>(key.data()), out_nonce.data(),
             const_cast<byte*>(ad.data()), static_cast<word32>(ad_len),
             const_cast<byte*>(in.data()), static_cast<word32>(in.size()),
             out_payload.data(), out_tag.data()) == 0;
}

bool aead_decrypt_frame(uint16_t cmd_id, uint16_t seq_id,
                        etl::span<const uint8_t> in,
                        etl::span<const uint8_t> key,
                        etl::span<const uint8_t> nonce,
                        etl::span<const uint8_t> tag,
                        etl::span<uint8_t> out_payload) {
  etl::array<uint8_t, 32> ad;
  const size_t ad_len = build_aad(cmd_id, seq_id, etl::span<uint8_t>(ad));

  return wc_ChaCha20Poly1305_Decrypt(
             const_cast<byte*>(key.data()), const_cast<byte*>(nonce.data()),
             const_cast<byte*>(ad.data()), static_cast<word32>(ad_len),
             const_cast<byte*>(in.data()), static_cast<word32>(in.size()),
             const_cast<byte*>(tag.data()), out_payload.data()) == 0;
}

bool validate_frame_nonce(etl::span<const uint8_t> nonce,
                          uint64_t* last_seen_counter) {
  if (nonce.size() < 12) return false;
  const auto nonce_sub = nonce.subspan(4);
  etl::byte_stream_reader n_reader(nonce_sub.data(), nonce_sub.size(),
                                   etl::endian::big);
  const uint64_t counter = n_reader.read<uint64_t>().value();
  if (last_seen_counter && counter <= *last_seen_counter) {
    return false;
  }
  if (last_seen_counter) *last_seen_counter = counter;
  return true;
}

// --- Self-Tests Implementation ---

#if BRIDGE_ENABLE_POST_TESTS

namespace {

int aead_kat_encrypt(etl::span<const uint8_t> key,
                     etl::span<const uint8_t> nonce,
                     etl::span<const uint8_t> ad, etl::span<const uint8_t> in,
                     etl::span<uint8_t> out, etl::span<uint8_t> tag) {
  return wc_ChaCha20Poly1305_Encrypt(
      const_cast<byte*>(key.data()), const_cast<byte*>(nonce.data()),
      const_cast<byte*>(ad.data()), static_cast<word32>(ad.size()),
      const_cast<byte*>(in.data()), static_cast<word32>(in.size()), out.data(),
      tag.data());
}

}  // namespace

static constexpr etl::array<uint8_t, 3> kat_sha256_msg PROGMEM = {
    {'a', 'b', 'c'}};
static constexpr etl::array<uint8_t, 32> kat_sha256_expected PROGMEM = {
    {0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
     0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
     0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD}};

static constexpr etl::array<uint8_t, 3> kat_hmac_key PROGMEM = {
    {'k', 'e', 'y'}};
static constexpr etl::array<uint8_t, 56> kat_hmac_data PROGMEM = {
    // "Jovencillo emponzoñado de whisky, qué figuritas exhibe"
    // Spanish pangram: 27/27 letters (a-z + ñ), 56 UTF-8 bytes
    {'J', 'o', 'v', 'e', 'n',  'c',  'i', 'l', 'l', 'o',  ' ',  'e', 'm', 'p',
     'o', 'n', 'z', 'o', 0xC3, 0xB1, 'a', 'd', 'o', ' ',  'd',  'e', ' ', 'w',
     'h', 'i', 's', 'k', 'y',  ',',  ' ', 'q', 'u', 0xC3, 0xA9, ' ', 'f', 'i',
     'g', 'u', 'r', 'i', 't',  'a',  's', ' ', 'e', 'x',  'h',  'i', 'b', 'e'}};
static constexpr etl::array<uint8_t, 32> kat_hmac_expected PROGMEM = {
    {0x53, 0x75, 0x96, 0x3F, 0x9E, 0x70, 0x9B, 0x58, 0x41, 0x50, 0x41,
     0xBA, 0xD2, 0xD4, 0x4D, 0xE2, 0x1F, 0x50, 0x80, 0x0E, 0x08, 0x41,
     0xB8, 0x7E, 0x0D, 0xAD, 0xFC, 0xDF, 0xE3, 0x62, 0xB2, 0x6C}};

// [SIL-2/H-1] NOT marked [[weak]]: cryptographic KATs MUST NOT be bypassable
// via linker substitution. Doing so would violate FIPS 140-3 requirements for
// Power-On Self-Tests. Use the test build flag to skip them instead.
bool run_cryptographic_self_tests() {
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> actual;
  etl::array<uint8_t, rpc::RPC_SHA256_KAT_BUFFER_SIZE> buffer;

  // 1. SHA256 KAT
  Sha256 sha;
  wc_InitSha256(&sha);
  const size_t msg_len = kat_sha256_msg.size();
  memcpy_P(buffer.data(), kat_sha256_msg.data(), msg_len);
  wc_Sha256Update(&sha, buffer.data(), static_cast<word32>(msg_len));
  wc_Sha256Final(&sha, actual.data());

  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> expected_buf;
  memcpy_P(expected_buf.data(), kat_sha256_expected.data(),
           rpc::RPC_SHA256_DIGEST_SIZE);
  bool ok = etl::equal(actual.begin(), actual.end(), expected_buf.begin());

  // 2. HMAC-SHA256 KAT
  Hmac hmac;
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> key_buf;
  const size_t key_len = kat_hmac_key.size();
  memcpy_P(key_buf.data(), kat_hmac_key.data(), key_len);

  wc_HmacSetKey(&hmac, WC_SHA256, key_buf.data(), static_cast<word32>(key_len));

  const size_t data_len = kat_hmac_data.size();
  memcpy_P(buffer.data(), kat_hmac_data.data(), data_len);
  wc_HmacUpdate(&hmac, buffer.data(), static_cast<word32>(data_len));
  wc_HmacFinal(&hmac, actual.data());

  memcpy_P(expected_buf.data(), kat_hmac_expected.data(),
           rpc::RPC_SHA256_DIGEST_SIZE);
  bool hmac_ok = etl::equal(actual.begin(), actual.end(), expected_buf.begin());

  // 3. ChaCha20-Poly1305 KAT (RFC 8439)
  static constexpr etl::array<uint8_t, 32> kat_aead_key = {
      {0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8a,
       0x8b, 0x8c, 0x8d, 0x8e, 0x8f, 0x90, 0x91, 0x92, 0x93, 0x94, 0x95,
       0x96, 0x97, 0x98, 0x99, 0x9a, 0x9b, 0x9c, 0x9d, 0x9e, 0x9f}};
  static constexpr etl::array<uint8_t, 12> kat_aead_nonce = {
      {0x07, 0x00, 0x00, 0x00, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47}};
  static constexpr etl::array<uint8_t, 12> kat_aead_ad = {
      {0x50, 0x51, 0x52, 0x53, 0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7}};
  static constexpr etl::array<uint8_t, 16> kat_aead_tag_expected = {
      {0x7d, 0xca, 0x84, 0x79, 0x78, 0x7a, 0x5c, 0x19, 0x0f, 0x58, 0xee, 0xda,
       0xe6, 0xa0, 0x6b, 0xcf}};

  etl::array<uint8_t, 16> aead_tag_actual;
  etl::array<uint8_t, 4> aead_out;
  int encrypt_res = aead_kat_encrypt(
      etl::span<const uint8_t>(kat_aead_key),
      etl::span<const uint8_t>(kat_aead_nonce),
      etl::span<const uint8_t>(kat_aead_ad),
      etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>("test"), 4),
      etl::span<uint8_t>(aead_out), etl::span<uint8_t>(aead_tag_actual));

  bool aead_res_ok = (encrypt_res == 0);
  bool aead_tag_ok = etl::equal(aead_tag_actual.begin(), aead_tag_actual.end(),
                                kat_aead_tag_expected.begin());
  const uint8_t val_ok = static_cast<uint8_t>(ok);
  const uint8_t val_hmac = static_cast<uint8_t>(hmac_ok);
  const uint8_t val_aead_res = static_cast<uint8_t>(aead_res_ok);
  const uint8_t val_aead_tag = static_cast<uint8_t>(aead_tag_ok);
  return (val_ok & val_hmac & val_aead_res & val_aead_tag) != 0U;
}

#endif  // BRIDGE_ENABLE_POST_TESTS

}  // namespace security
}  // namespace rpc
