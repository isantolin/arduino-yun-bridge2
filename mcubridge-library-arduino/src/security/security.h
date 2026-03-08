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

#include "sha256.h"
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "etl/algorithm.h"
#include "../protocol/rpc_protocol.h"

namespace rpc {
namespace security {

/// Number of bits per byte (used for counter-to-byte shifting).
constexpr int kBitsPerByte = 8;

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
 * @brief HMAC-SHA256 helper.
 */
inline void hmac_sha256(const uint8_t* key, size_t keyLen, const uint8_t* data,
                        size_t dataLen, uint8_t* out, size_t outLen = 32) {
  SHA256 hash;
  hash.resetHMAC(key, keyLen);
  hash.update(data, dataLen);
  hash.finalizeHMAC(key, keyLen, out, outLen);
}

/**
 * @brief HKDF-SHA256 Extract and Expand.
 */
void hkdf_sha256(const uint8_t* ikm, size_t ikm_len, 
                 const uint8_t* salt, size_t salt_len, 
                 const uint8_t* info, size_t info_len, 
                 uint8_t* okm, size_t okm_len);

/**
 * @brief Derive the handshake authentication key.
 */
void derive_handshake_key(const uint8_t* secret, size_t secret_len, uint8_t* out_key);

/**
 * @brief Generate nonce with monotonic counter (anti-replay).
 */
template <typename RandomFunc>
inline void generate_nonce_with_counter(uint8_t* out_nonce, uint64_t& counter,
                                        RandomFunc random_func) {
  // [SIL-2] Use ETL algorithm for deterministic generation
  etl::generate(out_nonce, out_nonce + rpc::RPC_HANDSHAKE_NONCE_RANDOM_BYTES,
                [&]() { return static_cast<uint8_t>(random_func() & 0xFF); });

  counter++;
  for (unsigned i = 0; i < rpc::RPC_HANDSHAKE_NONCE_COUNTER_BYTES; i++) {
    out_nonce[(rpc::RPC_HANDSHAKE_NONCE_LENGTH - 1) - i] =
        static_cast<uint8_t>((counter >> (i * kBitsPerByte)) & 0xFF);
  }
}

/**
 * @brief Extract counter from nonce (for validation).
 */
inline uint64_t extract_nonce_counter(const uint8_t* nonce) {
  return etl::accumulate(nonce + rpc::RPC_HANDSHAKE_NONCE_RANDOM_BYTES,
                         nonce + rpc::RPC_HANDSHAKE_NONCE_LENGTH, 0ULL,
                         [](uint64_t acc, uint8_t byte) {
                           return (acc << kBitsPerByte) | byte;
                         });
}

/**
 * @brief Validate nonce counter is strictly greater than last seen.
 */
inline bool validate_nonce_counter(const uint8_t* nonce,
                                   uint64_t& last_counter) {
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
