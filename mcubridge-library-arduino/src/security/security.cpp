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
#include <Arduino.h>
#include <wolfssl/wolfcrypt/hash.h>
#include <wolfssl/wolfcrypt/hmac.h>
#include <wolfssl/wolfcrypt/kdf.h>
#include <string.h>
#include <etl/array.h>
#include <etl/algorithm.h>
#include "Bridge.h"

namespace rpc {
namespace security {

McuBridgeSha256::McuBridgeSha256() : is_hmac_active_(false) {
  reset();
}

void McuBridgeSha256::reset() {
  wc_InitSha256(&sha_);
  is_hmac_active_ = false;
}

void McuBridgeSha256::update(etl::span<const uint8_t> data) {
  if (is_hmac_active_) {
    wc_HmacUpdate(&hmac_, static_cast<const byte*>(data.data()), static_cast<word32>(data.size()));
  } else {
    wc_Sha256Update(&sha_, static_cast<const byte*>(data.data()), static_cast<word32>(data.size()));
  }
}

void McuBridgeSha256::_finalize_impl(uint8_t* hash, size_t len) {
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> full_digest;
  wc_Sha256Final(&sha_, full_digest.data());
  etl::copy(full_digest.begin(), full_digest.begin() + etl::min(len, static_cast<size_t>(rpc::RPC_SHA256_DIGEST_SIZE)), hash);
}

void McuBridgeSha256::resetHMAC(etl::span<const uint8_t> key) {
  wc_HmacSetKey(&hmac_, WC_SHA256, static_cast<const byte*>(key.data()), static_cast<word32>(key.size()));
  is_hmac_active_ = true;
}

void McuBridgeSha256::_finalize_hmac_impl(uint8_t* hash, size_t len) {
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> full_digest;
  wc_HmacFinal(&hmac_, full_digest.data());
  etl::copy(full_digest.begin(), full_digest.begin() + etl::min(len, static_cast<size_t>(rpc::RPC_SHA256_DIGEST_SIZE)), hash);
  is_hmac_active_ = false;
}

// --- HKDF Implementation ---

void hkdf_sha256(etl::span<uint8_t> out, etl::span<const uint8_t> key,
                 etl::span<const uint8_t> salt,
                 etl::span<const uint8_t> info) {
  
  wc_HKDF(WC_SHA256,
          key.data(), static_cast<word32>(key.size()),
          salt.data(), static_cast<word32>(salt.size()),
          info.data(), static_cast<word32>(info.size()),
          out.data(), static_cast<word32>(out.size()));
}

// --- Self-Tests Implementation ---

static constexpr uint8_t kat_sha256_msg[] PROGMEM = {'a', 'b', 'c'};
static constexpr uint8_t kat_sha256_expected[] PROGMEM = {
    0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
    0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
    0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD};

static constexpr uint8_t kat_hmac_key[] PROGMEM = {'k', 'e', 'y'};
static constexpr uint8_t kat_hmac_data[] PROGMEM = {
    'T', 'h', 'e', ' ', 'q', 'u', 'i', 'c', 'k', ' ', 'b', 'r', 'o', 'w', 'n',
    ' ', 'f', 'o', 'x', ' ', 'j', 'u', 'm', 'p', 's', ' ', 'o', 'v', 'e', 'r',
    ' ', 't', 'h', 'e', ' ', 'l', 'a', 'z', 'y', ' ', 'd', 'o', 'g'};
static constexpr uint8_t kat_hmac_expected[] PROGMEM = {
    0xF7, 0xBC, 0x83, 0xF4, 0x30, 0x53, 0x84, 0x24, 0xB1, 0x32, 0x98,
    0xE6, 0xAA, 0x6F, 0xB1, 0x43, 0xEF, 0x4D, 0x59, 0xA1, 0x49, 0x46,
    0x17, 0x59, 0x97, 0x47, 0x9D, 0xBC, 0x2D, 0x1A, 0x3C, 0xD8};

bool run_cryptographic_self_tests() {
  McuBridgeSha256 sha256;
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> actual;
  etl::array<uint8_t, rpc::RPC_SHA256_KAT_BUFFER_SIZE> buffer;

  // 1. SHA256 KAT
  sha256.reset();
  size_t msg_len = sizeof(kat_sha256_msg);
  memcpy_P(buffer.data(), kat_sha256_msg, msg_len);
  sha256.update(etl::span<const uint8_t>(buffer.data(), msg_len));
  sha256.finalize(actual);

  if (memcmp_P(actual.data(), kat_sha256_expected, rpc::RPC_SHA256_DIGEST_SIZE) != 0)
    return false;

  // 2. HMAC-SHA256 KAT
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> key_buf;
  size_t key_len = sizeof(kat_hmac_key);
  memcpy_P(key_buf.data(), kat_hmac_key, key_len);

  sha256.resetHMAC(etl::span<const uint8_t>(key_buf.data(), key_len));

  size_t data_len = sizeof(kat_hmac_data);
  memcpy_P(buffer.data(), kat_hmac_data, data_len);
  sha256.update(etl::span<const uint8_t>(buffer.data(), data_len));
  sha256.finalizeHMAC(actual);

  if (memcmp_P(actual.data(), kat_hmac_expected, rpc::RPC_SHA256_DIGEST_SIZE) != 0)
    return false;

  return true;
}

}  // namespace security
}  // namespace rpc
