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
#ifndef PROGMEM
#define PROGMEM
#endif
#ifndef pgm_read_byte
#define pgm_read_byte(addr) (*(const unsigned char *)(addr))
#endif
#ifndef memcpy_P
#define memcpy_P memcpy
#endif
#ifndef memcmp_P
#define memcmp_P memcmp
#endif
#endif

namespace rpc {
namespace security {

/// SHA-256 digest size in bytes.
constexpr size_t kSha256DigestSize = 32;
/// KAT scratch buffer: large enough for both SHA-256 and HMAC-SHA256 inputs.
constexpr size_t kKatBufferSize = kSha256DigestSize * 2;

/**
 * [MIL-SPEC] Known Answer Test (KAT) Vectors.
 * Stored in Flash (PROGMEM) to save RAM on memory-constrained MCUs.
 */
static const uint8_t kat_sha256_msg[] PROGMEM = {'a', 'b', 'c'};
static const uint8_t kat_sha256_expected[] PROGMEM = {
    0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
    0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
    0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD};

static const uint8_t kat_hmac_key[] PROGMEM = {'k', 'e', 'y'};
static const uint8_t kat_hmac_data[] PROGMEM = {
    'T', 'h', 'e', ' ', 'q', 'u', 'i', 'c', 'k', ' ', 'b', 'r', 'o', 'w', 'n',
    ' ', 'f', 'o', 'x', ' ', 'j', 'u', 'm', 'p', 's', ' ', 'o', 'v', 'e', 'r',
    ' ', 't', 'h', 'e', ' ', 'l', 'a', 'z', 'y', ' ', 'd', 'o', 'g'};
static const uint8_t kat_hmac_expected[] PROGMEM = {
    0xF7, 0xBC, 0x83, 0xF4, 0x30, 0x53, 0x84, 0x24, 0xB1, 0x32, 0x98,
    0xE6, 0xAA, 0x6F, 0xB1, 0x43, 0xEF, 0x4D, 0x59, 0xA1, 0x49, 0x46,
    0x17, 0x59, 0x97, 0x47, 0x9D, 0xBC, 0x2D, 0x1A, 0x3C, 0xD8};

bool run_cryptographic_self_tests() {
  SHA256 sha256;
  etl::array<uint8_t, kSha256DigestSize> actual;
  etl::array<uint8_t, kKatBufferSize> buffer; // Temporary buffer for data loading

  // 1. SHA256 KAT ("abc")
  size_t msg_len = sizeof(kat_sha256_msg);
  memcpy_P(buffer.data(), kat_sha256_msg, msg_len);
  sha256.update(buffer.data(), msg_len);
  sha256.finalize(actual.data(), kSha256DigestSize);

  // Compare actual vs expected (expected is in PROGMEM)
  // etl::equal might work if we provide a custom iterator or just use memcmp_P
  if (memcmp_P(actual.data(), kat_sha256_expected, kSha256DigestSize) != 0) return false;

  // 2. HMAC-SHA256 KAT
  // Key is small, can load to stack or use update with PROGMEM if lib supports it. 
  // SHA256 lib usually supports RAM only.
  etl::array<uint8_t, 32> key_buf; // Max key size for test
  size_t key_len = sizeof(kat_hmac_key);
  memcpy_P(key_buf.data(), kat_hmac_key, key_len);
  
  sha256.resetHMAC(key_buf.data(), key_len); 
  
  size_t data_len = sizeof(kat_hmac_data);
  memcpy_P(buffer.data(), kat_hmac_data, data_len);
  sha256.update(buffer.data(), data_len);
  sha256.finalizeHMAC(key_buf.data(), key_len, actual.data(), kSha256DigestSize);

  if (memcmp_P(actual.data(), kat_hmac_expected, kSha256DigestSize) != 0) return false;

  return true;
}

} // namespace security
} // namespace rpc
