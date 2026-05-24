/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 */
#include "security.h"

#include <etl/algorithm.h>
#include <etl/byte_stream.h>
#include <etl/span.h>

#if defined(BRIDGE_HOST_TEST) && defined(BRIDGE_FAULT_INJECTION)
#include "BridgeFaultInjection.h"
#endif

#include "Bridge.h"
#include "hal/hal.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

#include <wolfssl/wolfcrypt/chacha20_poly1305.h>

namespace rpc {
namespace security {

namespace {
using bridge::hal::memory_fence;

// [MIL-SPEC] Known Answer Test (KAT) Data - NIST SP 800-22 / FIPS 140-3
const etl::array<uint8_t, 3> kat_sha256_msg RPC_PROGMEM = {'a', 'b', 'c'};
const etl::array<uint8_t, 32> kat_sha256_expected RPC_PROGMEM = {
    0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
    0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
    0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD};

const etl::array<uint8_t, 4> kat_hmac_key RPC_PROGMEM = {0x01, 0x02, 0x03,
                                                         0x04};
const etl::array<uint8_t, 4> kat_hmac_data RPC_PROGMEM = {0x05, 0x06, 0x07,
                                                          0x08};
const etl::array<uint8_t, 32> kat_hmac_expected RPC_PROGMEM = {
    0x9B, 0x09, 0xFF, 0x1E, 0x22, 0x0D, 0x7A, 0x16, 0x6F, 0x6C, 0xA1,
    0x37, 0x07, 0xA8, 0x0C, 0x6F, 0x67, 0x63, 0x9B, 0x00, 0x46, 0x17,
    0x57, 0x98, 0x25, 0x9A, 0xC6, 0x9B, 0x49, 0x9D, 0xAA, 0x7E};

}  // namespace

void hkdf_sha256(etl::span<uint8_t> out_key, etl::span<const uint8_t> secret,
                 etl::span<const uint8_t> salt, etl::span<const uint8_t> info) {
  (void)wc_HKDF(WC_SHA256, secret.data(), static_cast<word32>(secret.size()),
                salt.data(), static_cast<word32>(salt.size()), info.data(),
                static_cast<word32>(info.size()), out_key.data(),
                static_cast<word32>(out_key.size()));
}

bool aead_encrypt(etl::span<uint8_t> out, etl::span<uint8_t> out_tag,
                  etl::span<const uint8_t> in, etl::span<const uint8_t> key,
                  etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> ad) {
  return wc_ChaCha20Poly1305_Encrypt(
             key.data(), nonce.data(), ad.data(), static_cast<word32>(ad.size()),
             in.data(), static_cast<word32>(in.size()), out.data(),
             out_tag.data()) == 0;
}

bool aead_decrypt(etl::span<uint8_t> out, etl::span<const uint8_t> in,
                  etl::span<const uint8_t> tag, etl::span<const uint8_t> key,
                  etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> ad) {
  return wc_ChaCha20Poly1305_Decrypt(
             key.data(), nonce.data(), ad.data(), static_cast<word32>(ad.size()),
             in.data(), static_cast<word32>(in.size()), tag.data(),
             out.data()) == 0;
}

bool handshake_authenticate_raw(const uint8_t* secret, size_t secret_len,
                                 const uint8_t* nonce, size_t nonce_len,
                                 const uint8_t* received_tag, size_t tag_len,
                                 uint8_t* out_tag) {
  etl::array<uint8_t, rpc::HANDSHAKE_HKDF_OUTPUT_LENGTH> handshake_key;
  hkdf_sha256(etl::span<uint8_t>(handshake_key),
              etl::span<const uint8_t>(secret, secret_len),
              etl::span<const uint8_t>(rpc::HANDSHAKE_HKDF_SALT),
              etl::span<const uint8_t>(rpc::HANDSHAKE_HKDF_INFO_AUTH));

  Hmac hmac_engine;
  wc_HmacSetKey(&hmac_engine, WC_SHA256, handshake_key.data(),
                rpc::HANDSHAKE_HKDF_OUTPUT_LENGTH);
  wc_HmacUpdate(&hmac_engine, nonce, static_cast<word32>(nonce_len));
  wc_HmacFinal(&hmac_engine, out_tag);

  bool tag_ok = true;
  if (received_tag != nullptr && tag_len > 0) {
    tag_ok = timing_safe_equal(
        etl::span<const uint8_t>(out_tag, rpc::HANDSHAKE_TAG_LENGTH),
        etl::span<const uint8_t>(received_tag, tag_len));
  }

  secure_zero(handshake_key);
  return tag_ok;
}

void derive_session_key_raw(const uint8_t* secret, size_t secret_len,
                             const uint8_t* nonce, size_t nonce_len,
                             uint8_t* out_key) {
  hkdf_sha256(etl::span<uint8_t>(out_key, 32), etl::span<const uint8_t>(secret, secret_len),
              etl::span<const uint8_t>(nonce, nonce_len),
              etl::span<const uint8_t>(rpc::HANDSHAKE_HKDF_INFO_SESSION));
}

bool aead_encrypt_frame(uint16_t cmd_id, uint16_t seq_id, 
                        etl::span<const uint8_t> in,
                        etl::span<const uint8_t> key,
                        uint64_t& nonce_counter,
                        etl::span<uint8_t> out_payload,
                        etl::span<uint8_t> out_nonce,
                        etl::span<uint8_t> out_tag) {
  nonce_counter++;
  etl::fill(out_nonce.begin(), out_nonce.end(), 0U);
  constexpr etl::string_view mcu_prefix("MCU");
  etl::copy_n(mcu_prefix.begin(), 3, out_nonce.begin());
  etl::byte_stream_writer n_writer(out_nonce.data() + 4, 8, etl::endian::big);
  n_writer.write<uint64_t>(nonce_counter);

  rpc_pb_RpcEnvelope aad_env = rpc_pb_RpcEnvelope_init_default;
  aad_env.version = rpc::PROTOCOL_VERSION;
  aad_env.command_id = cmd_id;
  aad_env.sequence_id = seq_id;

  etl::array<uint8_t, 32> ad;
  ad.fill(0U);
  pb_ostream_t stream = pb_ostream_from_buffer(ad.data(), ad.size());
  (void)pb_encode(&stream, rpc_pb_RpcEnvelope_fields, &aad_env);

  return aead_encrypt(out_payload, out_tag, in, key, out_nonce,
                      etl::span<const uint8_t>(ad.data(), stream.bytes_written));
}

bool aead_decrypt_frame(uint16_t cmd_id, uint16_t seq_id,
                        etl::span<const uint8_t> in,
                        etl::span<const uint8_t> tag,
                        etl::span<const uint8_t> key,
                        etl::span<const uint8_t> nonce,
                        etl::span<uint8_t> out_payload) {
  rpc_pb_RpcEnvelope aad_env = rpc_pb_RpcEnvelope_init_default;
  aad_env.version = rpc::PROTOCOL_VERSION;
  aad_env.command_id = cmd_id;
  aad_env.sequence_id = seq_id;

  etl::array<uint8_t, 32> ad;
  ad.fill(0U);
  pb_ostream_t stream = pb_ostream_from_buffer(ad.data(), ad.size());
  (void)pb_encode(&stream, rpc_pb_RpcEnvelope_fields, &aad_env);

  return aead_decrypt(out_payload, in, tag, key, nonce,
                      etl::span<const uint8_t>(ad.data(), stream.bytes_written));
}

bool validate_frame_nonce(etl::span<const uint8_t> nonce, uint64_t& last_seen_counter) {
  if (nonce.size() < 12) return false;
  uint64_t counter = extract_nonce_counter(nonce);
  if (counter <= last_seen_counter) return false;
  last_seen_counter = counter;
  return true;
}

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
  if (!etl::equal(actual.begin(), actual.end(), expected_buf.begin()))
    return false;

  // 2. HMAC-SHA256 KAT
  Hmac hmac;
  const size_t key_len = kat_hmac_key.size();
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> key_buf;
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

  return true;
}

}  // namespace security
}  // namespace rpc
