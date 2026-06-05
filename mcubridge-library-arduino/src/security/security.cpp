/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include "security.h"

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <wolfssl/wolfcrypt/chacha20_poly1305.h>
#include <wolfssl/wolfcrypt/hash.h>
#include <wolfssl/wolfcrypt/hmac.h>
#include <wolfssl/wolfcrypt/kdf.h>

#include "Bridge.h"
#include "hal/progmem_compat.h"




namespace rpc {
namespace security {

namespace {

int aead_kat_encrypt(etl::span<const uint8_t> key,
                     etl::span<const uint8_t> nonce,
                     etl::span<const uint8_t> ad, etl::span<const uint8_t> in,
                     etl::span<uint8_t> out, etl::span<uint8_t> tag) {
  return wc_ChaCha20Poly1305_Encrypt(
      const_cast<byte*>(key.data()), const_cast<byte*>(nonce.data()), 
      const_cast<byte*>(ad.data()), static_cast<word32>(ad.size()),
      const_cast<byte*>(in.data()), static_cast<word32>(in.size()), 
      out.data(), tag.data());
}

}  // namespace

// --- HKDF Implementation ---

void hkdf_sha256(etl::span<uint8_t> out, etl::span<const uint8_t> key,
                 etl::span<const uint8_t> salt, etl::span<const uint8_t> info) {
  wc_HKDF(WC_SHA256, key.data(), static_cast<word32>(key.size()), salt.data(),
          static_cast<word32>(salt.size()), info.data(),
          static_cast<word32>(info.size()), out.data(),
          static_cast<word32>(out.size()));
}

bool handshake_authenticate(etl::span<const uint8_t> secret,
                            etl::span<const uint8_t> nonce,
                            etl::span<const uint8_t> received_tag,
                            etl::span<uint8_t> out_tag) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> handshake_key;
  hkdf_sha256(etl::span<uint8_t>(handshake_key),
              secret,
              etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
              etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));

  Hmac hmac_engine;
  wc_HmacSetKey(&hmac_engine, WC_SHA256, handshake_key.data(),
                rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH);
  wc_HmacUpdate(&hmac_engine, nonce.data(), static_cast<word32>(nonce.size()));
  wc_HmacFinal(&hmac_engine, out_tag.data());

  bool tag_ok = true;
  if (!received_tag.empty()) {
    tag_ok = timing_safe_equal(
        etl::span<const uint8_t>(out_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH),
        received_tag);
  }
  secure_zero(handshake_key);
  return tag_ok;
}

void derive_session_key(etl::span<const uint8_t> secret,
                        etl::span<const uint8_t> nonce,
                        etl::span<uint8_t> out_key) {
  hkdf_sha256(out_key, secret, nonce,
              etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_SESSION));
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
  constexpr etl::string_view mcu_prefix("MCU");
  etl::copy_n(mcu_prefix.begin(), 3, out_nonce.begin());
  etl::byte_stream_writer n_writer(out_nonce.data() + 4, 8, etl::endian::big);
  n_writer.write<uint64_t>(current_nonce);

  payload::RpcEnvelope aad_env = {};
  aad_env.version = rpc::PROTOCOL_VERSION;
  aad_env.command_id = cmd_id;
  aad_env.sequence_id = seq_id;

  etl::array<uint8_t, 32> ad;
  ad.fill(0U);
  pb_ostream_t stream = pb_ostream_from_buffer(ad.data(), ad.size());
  if (!pb_encode(&stream, rpc::Payload::get_fields<rpc_pb_RpcEnvelope>(), &aad_env)) {
    return false;
  }

  return wc_ChaCha20Poly1305_Encrypt(const_cast<byte*>(key.data()), out_nonce.data(), 
                                     const_cast<byte*>(ad.data()), static_cast<word32>(stream.bytes_written), 
                                     const_cast<byte*>(in.data()), static_cast<word32>(in.size()), 
                                     out_payload.data(), out_tag.data()) == 0;
}

bool aead_decrypt_frame(uint16_t cmd_id, uint16_t seq_id,
                        etl::span<const uint8_t> in,
                        etl::span<const uint8_t> tag,
                        etl::span<const uint8_t> key,
                        etl::span<const uint8_t> nonce,
                        etl::span<uint8_t> out_payload) {
  payload::RpcEnvelope aad_env = {};
  aad_env.version = rpc::PROTOCOL_VERSION;
  aad_env.command_id = cmd_id;
  aad_env.sequence_id = seq_id;

  etl::array<uint8_t, 32> ad;
  ad.fill(0U);
  pb_ostream_t stream = pb_ostream_from_buffer(ad.data(), ad.size());
  if (!pb_encode(&stream, rpc::Payload::get_fields<rpc_pb_RpcEnvelope>(), &aad_env)) {
    return false;
  }

  return wc_ChaCha20Poly1305_Decrypt(const_cast<byte*>(key.data()), const_cast<byte*>(nonce.data()), 
                                     const_cast<byte*>(ad.data()), static_cast<word32>(stream.bytes_written), 
                                     const_cast<byte*>(in.data()), static_cast<word32>(in.size()), 
                                     const_cast<byte*>(tag.data()), out_payload.data()) == 0;
}

bool validate_frame_nonce(etl::span<const uint8_t> nonce,
                          uint64_t* last_seen_counter) {
  if (nonce.size() < 12) return false;
  uint64_t counter = 0;
  etl::byte_stream_reader n_reader(nonce.data() + 4, 8, etl::endian::big);
  if (auto c_opt = n_reader.read<uint64_t>()) {
    counter = *c_opt;
  }
  if (last_seen_counter && counter <= *last_seen_counter) {
    return false;
  }
  if (last_seen_counter) *last_seen_counter = counter;
  return true;
}

// --- Self-Tests Implementation ---

static constexpr etl::array<uint8_t, 3> kat_sha256_msg PROGMEM = {
    {'a', 'b', 'c'}};
static constexpr etl::array<uint8_t, 32> kat_sha256_expected PROGMEM = {
    {0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
     0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
     0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD}};

static constexpr etl::array<uint8_t, 3> kat_hmac_key PROGMEM = {
    {'k', 'e', 'y'}};
static constexpr etl::array<uint8_t, 43> kat_hmac_data PROGMEM = {
    {'T', 'h', 'e', ' ', 'q', 'u', 'i', 'c', 'k', ' ', 'b', 'r', 'o', 'w',
     'n', ' ', 'f', 'o', 'x', ' ', 'j', 'u', 'm', 'p', 's', ' ', 'o', 'v',
     'e', 'r', ' ', 't', 'h', 'e', ' ', 'l', 'a', 'z', 'y', ' ', 'd', 'o',
     'g'}};
static constexpr etl::array<uint8_t, 32> kat_hmac_expected PROGMEM = {
    {0xF7, 0xBC, 0x83, 0xF4, 0x30, 0x53, 0x84, 0x24, 0xB1, 0x32, 0x98,
     0xE6, 0xAA, 0x6F, 0xB1, 0x43, 0xEF, 0x4D, 0x59, 0xA1, 0x49, 0x46,
     0x17, 0x59, 0x97, 0x47, 0x9D, 0xBC, 0x2D, 0x1A, 0x3C, 0xD8}};

bool __attribute__((weak)) run_cryptographic_self_tests() {
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
  if (!etl::equal(actual.begin(), actual.end(), expected_buf.begin()))
    return false;

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
  if (!etl::equal(actual.begin(), actual.end(), expected_buf.begin()))
    return false;

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
      {0x1a, 0xda, 0x1f, 0xf0, 0x62, 0x73, 0x8a, 0xda, 0x38, 0x39, 0xb9, 0x73,
       0x40, 0x73, 0x43, 0xac}};

  etl::array<uint8_t, 16> aead_tag_actual;
  etl::array<uint8_t, 4> aead_out;
  if (aead_kat_encrypt(
          etl::span<const uint8_t>(kat_aead_key),
          etl::span<const uint8_t>(kat_aead_nonce),
          etl::span<const uint8_t>(kat_aead_ad),
          etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>("test"), 4),
          etl::span<uint8_t>(aead_out),
          etl::span<uint8_t>(aead_tag_actual)) != 0)
    return false;

  return etl::equal(aead_tag_actual.begin(), aead_tag_actual.end(), kat_aead_tag_expected.begin());
}

}  // namespace security
}  // namespace rpc
