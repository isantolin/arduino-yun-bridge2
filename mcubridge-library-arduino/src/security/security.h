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
 * @brief AEAD encryption (ChaCha20-Poly1305).
 * out must be at least in.size() + tag.size().
 */
bool aead_encrypt(etl::span<uint8_t> out, etl::span<uint8_t> tag,
                  etl::span<const uint8_t> in, etl::span<const uint8_t> key,
                  etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> ad = {});

/**
 * @brief AEAD decryption (ChaCha20-Poly1305).
 * out must be at least in.size().
 */
bool aead_decrypt(etl::span<uint8_t> out, etl::span<const uint8_t> in,
                  etl::span<const uint8_t> tag, etl::span<const uint8_t> key,
                  etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> ad = {});

/**
 * @brief Perform timing-safe HMAC-SHA256 handshake authentication.
 * [MEM-SAVE] Centralizing this logic avoids duplication in BridgeClass handlers.
 */
bool handshake_authenticate_raw(const uint8_t* secret, size_t secret_len,
                                const uint8_t* nonce, size_t nonce_len,
                                const uint8_t* received_tag, size_t tag_len,
                                uint8_t* out_tag);

[[maybe_unused]] inline bool handshake_authenticate(etl::span<const uint8_t> secret,
                                   etl::span<const uint8_t> nonce,
                                   etl::span<const uint8_t> received_tag,
                                   etl::span<uint8_t> out_tag) {
  return handshake_authenticate_raw(secret.data(), secret.size(), nonce.data(),
                                     nonce.size(), received_tag.data(),
                                     received_tag.size(), out_tag.data());
}

/**
 * @brief Derive session key from shared secret and nonce using HKDF.
 */
void derive_session_key_raw(const uint8_t* secret, size_t secret_len,
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
  auto it_b = b.begin();
  [[maybe_unused]] auto _ = etl::for_each(a.begin(), a.end(),
                      [&](uint8_t val_a) { result |= val_a ^ *it_b++; });
  return result == 0;
}

/**
 * @brief Extract counter from nonce (for validation).
 */
inline uint64_t extract_nonce_counter(etl::span<const uint8_t> nonce) {
  if (nonce.size() < RPC_HANDSHAKE_NONCE_LENGTH) return 0;
  etl::byte_stream_reader r(nonce.data() + RPC_HANDSHAKE_NONCE_RANDOM_BYTES,
                            nonce.size() - RPC_HANDSHAKE_NONCE_RANDOM_BYTES,
                            etl::endian::big);
  return r.read<uint64_t>().value_or(0);
}

}  // namespace security
}  // namespace rpc

#endif  // RPC_SECURITY_H
