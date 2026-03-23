#include "rpc_cobs.h"
#include <etl/algorithm.h>
#include <etl/iterator.h>

namespace rpc {
namespace cobs {

size_t encode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
    if (src.empty() || dst.size() < (src.size() + src.size() / COBS_MAX_BLOCK_SIZE + 2)) {
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

size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
    if (src.empty() || dst.empty()) {
        return 0;
    }

    auto src_it = src.begin();
    auto dst_it = dst.begin();

    while (src_it != src.end()) {
        uint8_t code = *src_it++;
        if (code == 0) break;

        uint8_t payload_len = code - 1;
        if (static_cast<size_t>(etl::distance(src_it, src.end())) < payload_len ||
            static_cast<size_t>(etl::distance(dst_it, dst.end())) < payload_len) {
            return 0;
        }

        dst_it = etl::copy_n(src_it, payload_len, dst_it);
        src_it += payload_len;

        if (code < 0xFF && src_it != src.end()) {
            if (dst_it == dst.end()) return 0;
            *dst_it++ = 0;
        }
    }

    return etl::distance(dst.begin(), dst_it);
}

} // namespace cobs
} // namespace rpc
