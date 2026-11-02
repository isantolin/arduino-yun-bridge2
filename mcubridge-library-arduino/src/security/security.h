/**
 * @file security.h
 * @brief Link handshake authentication built on Pigweed SHA-256.
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

#include "protocol/rpc_protocol.h"
#include "pw_crypto/sha256.h"

namespace rpc {
namespace security {

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

/**
 * @brief Securely zero memory, resistant to compiler optimization.
 */
inline void secure_zero(etl::span<uint8_t> buf) {
  volatile uint8_t* p = buf.data();
  for (size_t i = 0; i < buf.size(); ++i) {
    p[i] = 0U;
  }
}

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
