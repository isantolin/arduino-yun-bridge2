/**
 * @file security.cpp
 * @brief Implementation of security primitives with memory optimizations.
 */
#include "security.h"
#include <Arduino.h>

#ifdef ARDUINO_ARCH_AVR
#include <avr/pgmspace.h>
#else
#ifndef PROGMEM
#define PROGMEM
#endif
#ifndef pgm_read_byte
#define pgm_read_byte(addr) (*(const unsigned char *)(addr))
#endif
#endif

namespace rpc {
namespace security {

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
  uint8_t actual[32];
  uint8_t buffer[64]; // Temporary buffer for data loading

  // 1. SHA256 KAT ("abc")
  size_t msg_len = sizeof(kat_sha256_msg);
  for(size_t i = 0; i < msg_len; i++) {
    buffer[i] = pgm_read_byte(&kat_sha256_msg[i]);
  }
  sha256.update(buffer, msg_len);
  sha256.finalize(actual, 32);
  
  for(size_t i = 0; i < 32; i++) {
    if (actual[i] != pgm_read_byte(&kat_sha256_expected[i])) return false;
  }

  // 2. HMAC-SHA256 KAT
  sha256.resetHMAC(kat_hmac_key, sizeof(kat_hmac_key)); // Note: resetHMAC copies key internally
  size_t data_len = sizeof(kat_hmac_data);
  // Process in chunks if data is large to save stack
  for(size_t i = 0; i < data_len; i++) {
    buffer[i] = pgm_read_byte(&kat_hmac_data[i]);
  }
  sha256.update(buffer, data_len);
  sha256.finalizeHMAC(kat_hmac_key, sizeof(kat_hmac_key), actual, 32);

  for(size_t i = 0; i < 32; i++) {
    if (actual[i] != pgm_read_byte(&kat_hmac_expected[i])) return false;
  }

  return true;
}

} // namespace security
} // namespace rpc
