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
#include <HKDF.h>

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
 * @brief Derive a key using HKDF-SHA256 (RFC 5869).
 * 
 * [MIL-SPEC] Uses OperatorFoundation/Crypto library implementation.
 * The library provides automatic secure cleanup via HKDF destructor.
 * 
 * @param ikm Input Keying Material (shared secret)
 * @param ikm_len Length of IKM
 * @param salt Optional salt (can be NULL)
 * @param salt_len Length of salt (0 if NULL)
 * @param info Application-specific context info
 * @param info_len Length of info
 * @param out_okm Output buffer for derived key
 * @param okm_len Desired output length (supports > 32 bytes)
 */
inline void hkdf_sha256(
    const uint8_t* ikm, size_t ikm_len,
    const uint8_t* salt, size_t salt_len,
    const uint8_t* info, size_t info_len,
    uint8_t* out_okm, size_t okm_len) {
  // Use library's optimized HKDF implementation
  // The template function handles Extract+Expand and cleanup
  ::hkdf<SHA256>(out_okm, okm_len, ikm, ikm_len, salt, salt_len, info, info_len);
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
bool run_cryptographic_self_tests();

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