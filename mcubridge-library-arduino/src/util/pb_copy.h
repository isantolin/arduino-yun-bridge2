#ifndef PB_COPY_H
#define PB_COPY_H

#include "etl/algorithm.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "nanopb/pb.h"

namespace rpc {
namespace util {

/**
 * @brief Copy a string_view into a nanopb fixed char array.
 *
 * Copies at most (dst_size - 1) characters, leaving room for the null
 * terminator already present in the zero-initialised nanopb struct.
 */
inline void pb_copy_string(etl::string_view src, char* dst, size_t dst_size) {
  etl::copy_n(src.data(), etl::min(src.length(), dst_size - 1), dst);
}

/**
 * @brief Copy a byte span into a nanopb bytes field (has .bytes + .size).
 *
 * Copies at most sizeof(field.bytes) bytes and sets field.size accordingly.
 */
template <typename PbBytesField>
inline void pb_copy_bytes(etl::span<const uint8_t> src, PbBytesField& field) {
  field.size = static_cast<pb_size_t>(etl::min(src.size(), sizeof(field.bytes)));
  etl::copy_n(src.data(), field.size, field.bytes);
}

} // namespace util
} // namespace rpc

#endif
