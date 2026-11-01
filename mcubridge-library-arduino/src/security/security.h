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

#include "protocol/rpc_protocol.h"

/* [WOLFSSL] Core headers */
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/hmac.h>
#include <wolfssl/wolfcrypt/memory.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/sha256.h>

namespace rpc {
namespace security {

/**
 * @brief Perform timing-safe HMAC-SHA256 handshake authentication.
 * [MEM-SAVE] Centralizing this logic avoids duplication in BridgeClass
 * handlers.
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
 * @brief Securely zero memory, resistant to compiler optimization.
 */
inline void secure_zero(etl::span<uint8_t> buf) {
  if (buf.empty()) return;
  wc_ForceZero(buf.data(), buf.size());
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
