/**
 * @file security.cpp
 * @brief Implementation of security primitives with memory optimizations.
 */
#include "security.h"

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/array.h>

#ifdef ARDUINO_ARCH_AVR
#include <avr/pgmspace.h>
#else
#ifndef PGM_P
#define PGM_P const char*
#endif
#ifndef strlen_P
#define strlen_P strlen
#endif
#ifndef memcmp_P
#define memcmp_P memcmp
#endif
#ifndef memcpy_P
#define memcpy_P memcpy
#endif
#endif

namespace rpc {
namespace security {

/**
 * @brief Known Answer Test vectors for FIPS 140-3 validation.
 */
static const uint8_t kat_sha256_input[] PROGMEM = "abc";
static const uint8_t kat_sha256_expected[] PROGMEM = {
    0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea, 0x41, 0x41, 0x40,
    0xde, 0x5d, 0xae, 0x22, 0x23, 0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17,
    0x7a, 0x9c, 0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00, 0x15, 0xad};

static const uint8_t kat_hmac_key[] PROGMEM = "key";
static const uint8_t kat_hmac_data[] PROGMEM =
    "The quick brown fox jumps over the lazy dog";
static const uint8_t kat_hmac_expected[] PROGMEM = {
    0xf7, 0xbc, 0x83, 0xf4, 0x30, 0x53, 0x84, 0x24, 0xb1, 0x32, 0x98,
    0xea, 0xa6, 0xfb, 0x14, 0x3e, 0xf4, 0xd5, 0x9a, 0x14, 0x94, 0x61,
    0x75, 0x99, 0x74, 0x79, 0xdb, 0xc2, 0xd1, 0xa3, 0xcd, 0x8b};

constexpr size_t kSha256DigestSize = 32;

void hkdf_sha256(const uint8_t* ikm, size_t ikm_len, const uint8_t* salt,
                 size_t salt_len, const uint8_t* info, size_t info_len,
                 uint8_t* okm, size_t okm_len) {
  if (okm_len > 32) return;

  uint8_t prk[32];
  hmac_sha256(salt, salt_len, ikm, ikm_len, prk, 32);

  uint8_t info_block[128]; 
  size_t actual_info_len = etl::min<size_t>(info_len, 127);
  if (actual_info_len > 0) memcpy(info_block, info, actual_info_len);
  info_block[actual_info_len] = 0x01;

  hmac_sha256(prk, 32, info_block, actual_info_len + 1, okm, okm_len);
  secure_zero(prk, 32);
}

void derive_handshake_key(const uint8_t* secret, size_t secret_len,
                          uint8_t* out_key) {
  hkdf_sha256(secret, secret_len, rpc::RPC_HANDSHAKE_HKDF_SALT,
              sizeof(rpc::RPC_HANDSHAKE_HKDF_SALT),
              rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH,
              sizeof(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH), out_key, 32);
}

bool run_cryptographic_self_tests() {
  SHA256 hash;
  etl::array<uint8_t, kSha256DigestSize> actual;

  // 1. SHA-256 KAT
  hash.reset();
  hash.update(kat_sha256_input, 3);
  hash.finalize(actual.data(), kSha256DigestSize);

  if (memcmp_P(actual.data(), kat_sha256_expected, kSha256DigestSize) != 0)
    return false;

  // 2. HMAC-SHA256 KAT
  etl::array<uint8_t, 16> key_buf;
  etl::array<uint8_t, 64> data_buf;
  size_t key_len = strlen_P(reinterpret_cast<PGM_P>(kat_hmac_key));
  size_t data_len = strlen_P(reinterpret_cast<PGM_P>(kat_hmac_data));

  memcpy_P(key_buf.data(), kat_hmac_key, key_len);
  memcpy_P(data_buf.data(), kat_hmac_data, data_len);

  hmac_sha256(key_buf.data(), key_len, data_buf.data(), data_len, actual.data(),
              kSha256DigestSize);

  if (memcmp_P(actual.data(), kat_hmac_expected, kSha256DigestSize) != 0)
    return false;

  return true;
}

}  // namespace security
}  // namespace rpc
