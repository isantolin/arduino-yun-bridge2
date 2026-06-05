/**
 * @file security.h
 * @brief Security primitives for military-grade cryptographic operations.
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 */
#ifndef RPC_SECURITY_H
#define RPC_SECURITY_H

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/byte_stream.h>
#include <etl/span.h>
#include <stddef.h>
#include <stdint.h>

#include "../etl_ext/CounterIterator.h"
#include "../protocol/rpc_frame.h"
#include "../protocol/rpc_protocol.h"

/* [WOLFSSL] Core headers */
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/hmac.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/sha256.h>

namespace rpc {
namespace security {

/**
 * @brief HKDF (RFC 5869) wrapping wolfCrypt wc_HKDF.
 */
void hkdf_sha256(etl::span<uint8_t> out, etl::span<const uint8_t> key,
                 etl::span<const uint8_t> salt, etl::span<const uint8_t> info);

/**
 * @brief Perform timing-safe HMAC-SHA256 handshake authentication.
 * [MEM-SAVE] Centralizing this logic avoids duplication in BridgeClass handlers.
 */
bool handshake_authenticate(etl::span<const uint8_t> secret,
                            etl::span<const uint8_t> nonce,
                            etl::span<const uint8_t> received_tag,
                            etl::span<uint8_t> out_tag);

/**
 * @brief Derive session key from shared secret and nonce using HKDF.
 */
void derive_session_key(etl::span<const uint8_t> secret,
                        etl::span<const uint8_t> nonce,
                        etl::span<uint8_t> out_key);

/**
 * @brief Securely encrypt a frame's payload and populate nonce/tag.
 */
bool aead_encrypt_frame(uint16_t cmd_id, uint16_t seq_id, 
                        etl::span<const uint8_t> in,
                        etl::span<const uint8_t> key,
                        uint64_t* nonce_counter,
                        etl::span<uint8_t> out_payload,
                        etl::span<uint8_t> out_nonce,
                        etl::span<uint8_t> out_tag);

/**
 * @brief Securely decrypt a frame's payload.
 */
bool aead_decrypt_frame(uint16_t cmd_id, uint16_t seq_id,
                        etl::span<const uint8_t> in,
                        etl::span<const uint8_t> tag,
                        etl::span<const uint8_t> key,
                        etl::span<const uint8_t> nonce,
                        etl::span<uint8_t> out_payload);

/**
 * @brief Validate monotonic nonce counter to prevent replay attacks.
 */
bool validate_frame_nonce(etl::span<const uint8_t> nonce, uint64_t* last_seen_counter);

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
  using bridge::etl_ext::CounterIterator;
  etl::for_each(CounterIterator<size_t>(0U), CounterIterator<size_t>(a.size()),
                [&](size_t i) {
                  result |= (a[i] ^ b[i]);
                });
  return result == 0;
}

}  // namespace security
}  // namespace rpc

#endif // RPC_SECURITY_H
