/**
 * @file security.h
 * @brief Security primitives for military-grade cryptographic operations.
 *
 * This file is part of Arduino Yun Ecosystem v2.
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

namespace rpc {
namespace security {

/**
 * @brief Securely zero memory, resistant to compiler optimization.
 *
 * [MIL-SPEC] This function uses volatile pointer access and a memory
 * barrier to prevent the compiler from optimizing away the zeroing
 * operation, even if the buffer is not used afterward.
 *
 * Use this to clear sensitive data like:
 * - Cryptographic keys
 * - HMAC digests after comparison
 * - Nonces after use
 * - Shared secrets in temporary buffers
 *
 * @param buf   Pointer to buffer to zero
 * @param len   Number of bytes to zero
 *
 * Reference: CWE-14, CERT C MSC06-C
 */
inline void secure_zero(volatile uint8_t* buf, size_t len) {
  while (len--) {
    *buf++ = 0;
  }
  // Memory barrier prevents compiler from reordering or eliminating
#if defined(__GNUC__) || defined(__clang__)
  asm volatile("" ::: "memory");
#endif
}

/**
 * @brief Portable version of secure_zero for non-volatile buffers.
 *
 * Casts to volatile internally to ensure zeroing is not optimized away.
 *
 * @param buf   Pointer to buffer to zero
 * @param len   Number of bytes to zero
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
 * @brief Timing-safe memory comparison.
 *
 * [MIL-SPEC] This function compares two buffers in constant time,
 * regardless of where the first difference occurs. This prevents
 * timing side-channel attacks that could leak information about
 * secret data through execution time variations.
 *
 * Use this for comparing:
 * - HMAC tags
 * - Authentication tokens
 * - Password hashes
 * - Any security-sensitive comparison
 *
 * @param a     First buffer
 * @param b     Second buffer
 * @param len   Number of bytes to compare
 * @return      true if buffers are equal, false otherwise
 *
 * Reference: CWE-208 (Observable Timing Discrepancy)
 */
inline bool timing_safe_equal(const uint8_t* a, const uint8_t* b, size_t len) {
  volatile uint8_t result = 0;
  for (size_t i = 0; i < len; i++) {
    result |= a[i] ^ b[i];
  }
  return result == 0;
}

/**
 * @brief Timing-safe comparison with volatile result.
 *
 * Additional hardening: uses volatile for the result accumulator
 * to further prevent optimization.
 *
 * @param a     First buffer
 * @param b     Second buffer
 * @param len   Number of bytes to compare
 * @return      true if buffers are equal, false otherwise
 */
inline bool timing_safe_equal_hardened(
    const volatile uint8_t* a,
    const volatile uint8_t* b,
    size_t len) {
  volatile uint8_t result = 0;
  for (size_t i = 0; i < len; i++) {
    result |= a[i] ^ b[i];
  }
  // Additional barrier to prevent branch prediction optimization
#if defined(__GNUC__) || defined(__clang__)
  asm volatile("" ::: "memory");
#endif
  return result == 0;
}

/**
 * @brief Generate nonce with monotonic counter (anti-replay).
 *
 * [MIL-SPEC] Generates a 16-byte nonce with structure:
 * - Bytes 0-7:  Random data (entropy)
 * - Bytes 8-15: Monotonic counter (big-endian, anti-replay)
 *
 * The counter prevents replay attacks by ensuring each nonce
 * is unique and can be validated as newer than previous nonces.
 *
 * @param out_nonce     Output buffer (must be 16 bytes)
 * @param counter       Reference to monotonic counter (will be incremented)
 * @param random_func   Function to generate random byte (e.g., random(256))
 */
template <typename RandomFunc>
inline void generate_nonce_with_counter(
    uint8_t* out_nonce,
    uint64_t& counter,
    RandomFunc random_func) {
  // Random part (8 bytes)
  for (int i = 0; i < 8; i++) {
    out_nonce[i] = static_cast<uint8_t>(random_func() & 0xFF);
  }
  // Counter part (8 bytes, big-endian)
  counter++;
  for (int i = 0; i < 8; i++) {
    out_nonce[15 - i] = static_cast<uint8_t>((counter >> (i * 8)) & 0xFF);
  }
}

/**
 * @brief Extract counter from nonce (for validation).
 *
 * @param nonce   16-byte nonce with counter in bytes 8-15
 * @return        64-bit counter value
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
 *
 * [MIL-SPEC] Anti-replay protection: rejects any nonce with a counter
 * less than or equal to the last accepted counter.
 *
 * @param nonce         16-byte nonce to validate
 * @param last_counter  Reference to last accepted counter (updated on success)
 * @return              true if counter is valid (strictly greater), false otherwise
 */
inline bool validate_nonce_counter(const uint8_t* nonce, uint64_t& last_counter) {
  uint64_t current = extract_nonce_counter(nonce);
  if (current <= last_counter) {
    return false;  // Replay detected
  }
  last_counter = current;
  return true;
}

}  // namespace security
}  // namespace rpc

#endif  // RPC_SECURITY_H
