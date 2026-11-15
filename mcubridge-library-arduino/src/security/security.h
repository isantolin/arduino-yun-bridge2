/**
 * @file security.h
 * @brief Link handshake and frame cryptography backed by configured providers.
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 */
#ifndef RPC_SECURITY_H
#define RPC_SECURITY_H

#include <etl/delegate.h>
#include <pw_span/span.h>
#include <stddef.h>
#include <stdint.h>

namespace rpc {
namespace security {

/** Receives successive output chunks; returns false to abort the operation. */
using AeadOutputSink = etl::delegate<bool(pw::span<const uint8_t>)>;

/** Maximum plaintext accepted by aead_encrypt_chunked(). */
inline constexpr size_t kAeadChunkedMaxPlaintext = 512U;

/**
 * Derive host-to-MCU and MCU-to-host keys (handshake v2, ADR-0005):
 * HKDF-SHA256(secret, salt = host_nonce || mcu_nonce, info = session v2).
 * Writes RX (host-to-MCU) then TX (MCU-to-host) into one contiguous buffer.
 */
bool derive_session_keys(pw::span<const uint8_t> secret,
                         pw::span<const uint8_t> host_nonce,
                         pw::span<const uint8_t> mcu_nonce,
                         pw::span<uint8_t> session_keys);

/**
 * Derive the MCU's 16-byte handshake nonce from a never-repeating 16-byte
 * counter block (boot epoch || handshake counter) on boards without a
 * hardware RNG. Uniqueness comes from the counters; the keyed HMAC only
 * makes the value unpredictable without the secret.
 */
bool derive_mcu_nonce(pw::span<const uint8_t> secret,
                      pw::span<const uint8_t> counter_block,
                      pw::span<uint8_t> out_nonce);

/** Encrypt plaintext with ChaCha20-Poly1305. */
bool aead_encrypt(pw::span<const uint8_t> key, pw::span<const uint8_t> nonce,
                  pw::span<const uint8_t> aad,
                  pw::span<const uint8_t> plaintext,
                  pw::span<uint8_t> ciphertext, pw::span<uint8_t> tag);

/**
 * Encrypt plaintext with ChaCha20-Poly1305 in small chunks, passing each
 * ciphertext chunk and finally the 16-byte tag to `sink`.
 *
 * Produces exactly the ciphertext || tag of aead_encrypt() without a
 * plaintext-sized output buffer, so callers can stream it (e.g. into an HDLC
 * encoder) using only a fixed small stack chunk.
 */
bool aead_encrypt_chunked(pw::span<const uint8_t> key,
                          pw::span<const uint8_t> nonce,
                          pw::span<const uint8_t> aad,
                          pw::span<const uint8_t> plaintext,
                          const AeadOutputSink& sink);

/** Authenticate and decrypt ciphertext with ChaCha20-Poly1305. */
bool aead_decrypt(pw::span<const uint8_t> key, pw::span<const uint8_t> nonce,
                  pw::span<const uint8_t> aad,
                  pw::span<const uint8_t> ciphertext,
                  pw::span<const uint8_t> tag, pw::span<uint8_t> plaintext);

/** Generate the truncated HMAC-SHA256 tag for a valid handshake nonce. */
bool handshake_generate_tag(pw::span<const uint8_t> secret,
                            pw::span<const uint8_t> nonce,
                            pw::span<uint8_t> out_tag);

/** Generate the MCU proof over host_nonce || mcu_nonce (handshake v2). */
bool handshake_generate_response_tag(pw::span<const uint8_t> secret,
                                     pw::span<const uint8_t> host_nonce,
                                     pw::span<const uint8_t> mcu_nonce,
                                     pw::span<uint8_t> out_tag);

/** Verify the host's exact-length handshake tag using a timing-safe compare. */
bool handshake_verify_tag(pw::span<const uint8_t> secret,
                          pw::span<const uint8_t> nonce,
                          pw::span<const uint8_t> received_tag);

/** Securely erase memory through the platform crypto implementation. */
void secure_zero(pw::span<uint8_t> buf);

/**
 * @brief Known Answer Tests (KAT) for cryptographic primitives.
 * [SIL-2/AVR] Compiled only when BRIDGE_ENABLE_POST_TESTS=1.
 * @return true if all tests pass, false otherwise.
 */
bool run_cryptographic_self_tests();

}  // namespace security
}  // namespace rpc

#endif  // RPC_SECURITY_H
