/**
 * @file security.h
 * @brief Security primitives for military-grade cryptographic operations.
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 */
#ifndef RPC_SECURITY_H
#define RPC_SECURITY_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <etl/algorithm.h>
#include <etl/span.h>
#include <etl/array.h>
#include "../protocol/rpc_protocol.h"
#include "../protocol/rpc_frame.h"


/* [WOLFSSL] Core headers */
#include <wolfssl/wolfcrypt/sha256.h>
#include <wolfssl/wolfcrypt/hmac.h>

namespace rpc {
namespace security {

/**
 * @brief Wrapper class for wolfSSL SHA-256 and HMAC.
 */
class McuBridgeSha256 {
 public:
  static constexpr size_t HASH_SIZE = 32;
  static constexpr size_t BLOCK_SIZE = 64;

  McuBridgeSha256();

  void reset();
  void update(const void* data, size_t len);
  void finalize(void* hash, size_t len);

  void resetHMAC(const void* key, size_t keyLen);
  void finalizeHMAC(const void* key, size_t keyLen, void* hash, size_t hashLen);

 private:
  Sha256 sha_;
  Hmac hmac_;
  bool is_hmac_active_;
};

/**
 * @brief HKDF (RFC 5869) wrapping wolfCrypt wc_HKDF.
 */
void hkdf_sha256(etl::span<uint8_t> out, etl::span<const uint8_t> key,
                 etl::span<const uint8_t> salt,
                 etl::span<const uint8_t> info);

/**
 * @brief Securely zero memory, resistant to compiler optimization.
 */
inline void secure_zero(etl::span<uint8_t> buf) {
  if (buf.empty()) return;
  etl::fill(buf.begin(), buf.end(), 0);
#if defined(__GNUC__) || defined(__clang__)
  asm volatile("" : : "r"(buf.data()) : "memory");
#endif
}

/**
 * @brief Known Answer Tests (KAT) for cryptographic primitives.
 * @return true if all tests pass, false otherwise.
 */
bool run_cryptographic_self_tests();

/**
 * @brief Timing-safe memory comparison.
 */
inline bool timing_safe_equal(etl::span<const uint8_t> a,
                              etl::span<const uint8_t> b) {
  if (a.size() != b.size()) return false;
  volatile uint8_t result = 0;
  for (size_t i = 0; i < a.size(); i++) {
    result |= a[i] ^ b[i];
  }
  return result == 0;
}

/**
 * @brief Generate nonce with monotonic counter (anti-replay).
 */
template <typename RandomFunc>
inline void generate_nonce_with_counter(etl::span<uint8_t> out_nonce,
                                        uint64_t& counter,
                                        RandomFunc random_func) {
  if (out_nonce.size() < RPC_HANDSHAKE_NONCE_LENGTH) return;

  etl::generate(out_nonce.begin(),
                out_nonce.begin() + RPC_HANDSHAKE_NONCE_RANDOM_BYTES,
                [&]() { return static_cast<uint8_t>(random_func() & 0xFF); });

  counter++;
  rpc::write_u64_be(out_nonce.subspan(RPC_HANDSHAKE_NONCE_RANDOM_BYTES), counter);
}

/**
 * @brief Extract counter from nonce (for validation).
 */
inline uint64_t extract_nonce_counter(etl::span<const uint8_t> nonce) {
  if (nonce.size() < RPC_HANDSHAKE_NONCE_LENGTH) return 0;
  return rpc::read_u64_be(nonce.subspan(RPC_HANDSHAKE_NONCE_RANDOM_BYTES));
}

/**
 * @brief Validate nonce counter is strictly greater than last seen.
 */
inline bool validate_nonce_counter(etl::span<const uint8_t> nonce,
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
