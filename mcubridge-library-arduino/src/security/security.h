/**
 * @file security.h
 * @brief Link handshake and frame cryptography backed by configured providers.
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 */
#ifndef RPC_SECURITY_H
#define RPC_SECURITY_H

#include <etl/array.h>
#include <etl/span.h>
#include <stddef.h>
#include <stdint.h>

#include "protocol/rpc_protocol.h"

namespace rpc {
namespace security {

/** Derive host-to-MCU and MCU-to-host keys from the shared secret and nonce. */
bool derive_session_keys(etl::span<const uint8_t> secret,
                         etl::span<const uint8_t> nonce,
                         etl::span<uint8_t> host_to_mcu_key,
                         etl::span<uint8_t> mcu_to_host_key);

/** Encrypt plaintext with ChaCha20-Poly1305. */
bool aead_encrypt(etl::span<const uint8_t> key, etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> aad,
                  etl::span<const uint8_t> plaintext,
                  etl::span<uint8_t> ciphertext, etl::span<uint8_t> tag);

/** Authenticate and decrypt ciphertext with ChaCha20-Poly1305. */
bool aead_decrypt(etl::span<const uint8_t> key, etl::span<const uint8_t> nonce,
                  etl::span<const uint8_t> aad,
                  etl::span<const uint8_t> ciphertext,
                  etl::span<const uint8_t> tag, etl::span<uint8_t> plaintext);

/** Generate the truncated HMAC-SHA256 tag for a valid handshake nonce. */
bool handshake_generate_tag(etl::span<const uint8_t> secret,
                            etl::span<const uint8_t> nonce,
                            etl::span<uint8_t> out_tag);

/** Generate the domain-separated MCU proof for a valid handshake nonce. */
bool handshake_generate_response_tag(etl::span<const uint8_t> secret,
                                     etl::span<const uint8_t> nonce,
                                     etl::span<uint8_t> out_tag);

/** Verify the host's exact-length handshake tag using a timing-safe compare. */
bool handshake_verify_tag(etl::span<const uint8_t> secret,
                          etl::span<const uint8_t> nonce,
                          etl::span<const uint8_t> received_tag);

/** Securely erase memory through the platform crypto implementation. */
void secure_zero(etl::span<uint8_t> buf);

/**
 * @brief Known Answer Tests (KAT) for cryptographic primitives.
 * [SIL-2/AVR] Compiled only when BRIDGE_ENABLE_POST_TESTS=1.
 * @return true if all tests pass, false otherwise.
 */
#if BRIDGE_ENABLE_POST_TESTS
bool run_cryptographic_self_tests();
#endif

}  // namespace security
}  // namespace rpc

#endif  // RPC_SECURITY_H
