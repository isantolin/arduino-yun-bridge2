/**
 * @file security.h
 * @brief Security primitives for military-grade cryptographic operations.
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 *
 * [MIL-SPEC COMPLIANCE]
 * These functions implement security primitives resistant to:
 * - Compiler optimization (secure_zero)
 * - Timing side-channel attacks (timing_safe_equal)
 *
 * Reference standards:
 * - NIST SP 800-90A (secure random)
 * - FIPS 140-3 (cryptographic module requirements)
 * - CWE-14 (compiler removal of code to clear buffers)
 */
#ifndef RPC_SECURITY_H
#define RPC_SECURITY_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <SHA256.h>

namespace rpc {
namespace security {

/**
 * @brief Securely zero memory, resistant to compiler optimization.
 *
 * [MIL-SPEC] This function uses volatile pointer access and a memory
 * barrier to prevent the compiler from optimizing away the zeroing
 * operation, even if the buffer is not used afterward.
 */
inline void secure_zero(volatile uint8_t* buf, size_t len) {
  while (len--) {
    *buf++ = 0;
  }
#if defined(__GNUC__) || defined(__clang__)
  asm volatile("" ::: "memory");
#endif
}

/**
 * @brief Portable version of secure_zero for non-volatile buffers.
 */
inline void secure_zero_portable(void* buf, size_t len) {
  volatile uint8_t* p = static_cast<volatile uint8_t*>(buf);
  while (len--) {
    *p++ = 0;
  }
#if defined(__GNUC__) || defined(__clang__)
  asm volatile("" ::: "memory");
#endif
}

/**
 * @brief HKDF-SHA256 Extract function (RFC 5869).
 */
inline void hkdf_sha256_extract(
    const uint8_t* salt, size_t salt_len,
    const uint8_t* ikm, size_t ikm_len,
    uint8_t* out_prk) {
  SHA256 sha256;
  sha256.resetHMAC(salt, salt_len);
  sha256.update(ikm, ikm_len);
  sha256.finalizeHMAC(salt, salt_len, out_prk, 32);
}

/**
 * @brief HKDF-SHA256 Expand function (RFC 5869).
 * Currently supports output length <= 32 bytes (one block) for handshake needs.
 */
inline void hkdf_sha256_expand(
    const uint8_t* prk, size_t prk_len,
    const uint8_t* info, size_t info_len,
    uint8_t* out_okm, size_t okm_len) {
  if (okm_len > 32) return; 
  
  SHA256 sha256;
  sha256.resetHMAC(prk, prk_len);
  if (info && info_len > 0) {
    sha256.update(info, info_len);
  }
  uint8_t counter = 1;
  sha256.update(&counter, 1);
  uint8_t full_okm[32];
  sha256.finalizeHMAC(prk, prk_len, full_okm, 32);
  memcpy(out_okm, full_okm, okm_len);
  secure_zero(full_okm, 32);
}

/**
 * @brief Derive a key using HKDF-SHA256.
 * 
 * This is the primary entry point for MIL-SPEC key derivation.
 */
inline void hkdf_sha256(
    const uint8_t* ikm, size_t ikm_len,
    const uint8_t* salt, size_t salt_len,
    const uint8_t* info, size_t info_len,
    uint8_t* out_okm, size_t okm_len) {
  uint8_t prk[32];
  hkdf_sha256_extract(salt, salt_len, ikm, ikm_len, prk);
  hkdf_sha256_expand(prk, 32, info, info_len, out_okm, okm_len);
  secure_zero(prk, 32);
}

/**
 * @brief Known Answer Tests (KAT) for cryptographic primitives.
 *
 * [MIL-SPEC COMPLIANCE - FIPS 140-3]
 * Mandatory self-tests at startup to ensure the cryptographic engine
 * (SHA256/HMAC) is operating correctly.
 *
 * @return true if all tests pass, false otherwise.
 */
inline bool run_cryptographic_self_tests() {
  // 1. SHA256 KAT ("abc")
  SHA256 sha256;
  const uint8_t msg[] = {'a', 'b', 'c'};
  const uint8_t expected_sha[] = {
      0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
      0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
      0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD};
  uint8_t actual_sha[32];
  sha256.update(msg, sizeof(msg));
  sha256.finalize(actual_sha, sizeof(actual_sha));
  if (memcmp(actual_sha, expected_sha, 32) != 0) return false;

  // 2. HMAC-SHA256 KAT
  const uint8_t key[] = {'k', 'e', 'y'};
  const uint8_t data[] = {
      'T', 'h', 'e', ' ', 'q', 'u', 'i', 'c', 'k', ' ', 'b', 'r', 'o', 'w', 'n',
      ' ', 'f', 'o', 'x', ' ', 'j', 'u', 'm', 'p', 's', ' ', 'o', 'v', 'e', 'r',
      ' ', 't', 'h', 'e', ' ', 'l', 'a', 'z', 'y', ' ', 'd', 'o', 'g'};
  const uint8_t expected_hmac[] = {
      0xF7, 0xBC, 0x83, 0xF4, 0x30, 0x53, 0x84, 0x24, 0xB1, 0x32, 0x98,
      0xE6, 0xAA, 0x6F, 0xB1, 0x43, 0xEF, 0x4D, 0x59, 0xA1, 0x49, 0x46,
      0x17, 0x59, 0x97, 0x47, 0x9D, 0xBC, 0x2D, 0x1A, 0x3C, 0xD8};
  uint8_t actual_hmac[32];
  sha256.resetHMAC(key, sizeof(key));
  sha256.update(data, sizeof(data));
  sha256.finalizeHMAC(key, sizeof(key), actual_hmac, sizeof(actual_hmac));
  if (memcmp(actual_hmac, expected_hmac, 32) != 0) return false;

  return true;
}

/**
 * @brief Timing-safe memory comparison.
 */
inline bool timing_safe_equal(const uint8_t* a, const uint8_t* b, size_t len) {
  volatile uint8_t result = 0;
  for (size_t i = 0; i < len; i++) {
    result |= a[i] ^ b[i];
  }
  return result == 0;
}

/**
 * @brief Generate nonce with monotonic counter (anti-replay).
 */
template <typename RandomFunc>
inline void generate_nonce_with_counter(
    uint8_t* out_nonce,
    uint64_t& counter,
    RandomFunc random_func) {
  for (int i = 0; i < 8; i++) {
    out_nonce[i] = static_cast<uint8_t>(random_func() & 0xFF);
  }
  counter++;
  for (int i = 0; i < 8; i++) {
    out_nonce[15 - i] = static_cast<uint8_t>((counter >> (i * 8)) & 0xFF);
  }
}

/**
 * @brief Extract counter from nonce (for validation).
 */
inline uint64_t extract_nonce_counter(const uint8_t* nonce) {
  uint64_t counter = 0;
  for (int i = 0; i < 8; i++) {
    counter = (counter << 8) | nonce[8 + i];
  }
  return counter;
}

/**
 * @brief Validate nonce counter is strictly greater than last seen.
 */
inline bool validate_nonce_counter(const uint8_t* nonce, uint64_t& last_counter) {
  uint64_t current = extract_nonce_counter(nonce);
  if (current <= last_counter) {
    return false;
  }
  last_counter = current;
  return true;
}

}  // namespace security
}  // namespace rpc

#endif  // RPC_SECURITY_H