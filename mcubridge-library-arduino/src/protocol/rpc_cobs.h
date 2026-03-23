#ifndef RPC_COBS_H
#define RPC_COBS_H

#include <stddef.h>
#include <stdint.h>

#include <etl/span.h>

/**
 * @brief COBS (Consistent Overhead Byte Stuffing) implementation.
 * [SIL-2] Uses ETL algorithms and iterators to avoid manual byte loops.
 */
namespace rpc {
namespace cobs {

/// Maximum bytes in a single COBS block before a mandatory code byte (0xFE = 254).
static constexpr size_t COBS_MAX_BLOCK_SIZE = 0xFE;

/**
 * @brief Encodes a source buffer into a destination buffer using COBS.
 * @return The length of the encoded data, or 0 on error.
 */
size_t encode(etl::span<const uint8_t> src, etl::span<uint8_t> dst);

/**
 * @brief Decodes a COBS-encoded buffer into a destination buffer.
 * @return The length of the decoded data, or 0 on error.
 */
size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst);

} // namespace cobs
} // namespace rpc

#endif // RPC_COBS_H
