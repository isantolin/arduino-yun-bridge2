#ifndef RPC_COBS_H
#define RPC_COBS_H

#include <stddef.h>
#include <stdint.h>

#include "etl/algorithm.h"
#include "etl/span.h"

/**
 * @brief COBS (Consistent Overhead Byte Stuffing) implementation.
 * [SIL-2] Uses ETL algorithms and iterators to avoid manual byte loops.
 */
namespace rpc {
namespace cobs {

/**
 * @brief Encodes a source buffer into a destination buffer using COBS.
 * @return The length of the encoded data, or 0 on error.
 */
inline size_t encode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
    if (src.empty() || dst.size() < (src.size() + src.size() / 254 + 2)) {
        return 0;
    }

    auto src_it = src.begin();
    auto dst_it = dst.begin();
    auto code_it = dst_it++; // Placeholder for the block code

    uint8_t code = 1;

    while (src_it != src.end()) {
        if (*src_it == 0) {
            *code_it = code;
            code_it = dst_it++;
            code = 1;
        } else {
            *dst_it++ = *src_it;
            code++;
            if (code == 0xFF) {
                *code_it = code;
                code_it = dst_it++;
                code = 1;
            }
        }
        src_it++;
    }

    *code_it = code;
    return etl::distance(dst.begin(), dst_it);
}

/**
 * @brief Decodes a COBS-encoded buffer into a destination buffer.
 * @return The length of the decoded data, or 0 on error.
 */
inline size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
    if (src.empty() || dst.empty()) {
        return 0;
    }

    auto src_it = src.begin();
    auto dst_it = dst.begin();

    while (src_it != src.end()) {
        uint8_t code = *src_it++;
        if (code == 0) break; // End of packet marker (optional in some COBS impls)

        for (uint8_t i = 1; i < code; ++i) {
            if (src_it == src.end() || dst_it == dst.end()) return 0;
            *dst_it++ = *src_it++;
        }

        if (code < 0xFF && src_it != src.end()) {
            if (dst_it == dst.end()) return 0;
            *dst_it++ = 0;
        }
    }

    return etl::distance(dst.begin(), dst_it);
}

} // namespace cobs
} // namespace rpc

#endif // RPC_COBS_H
