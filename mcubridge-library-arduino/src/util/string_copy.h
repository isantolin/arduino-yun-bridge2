#ifndef STRING_COPY_H
#define STRING_COPY_H

#include <etl/algorithm.h>
#include <etl/span.h>
#include <etl/string_view.h>

#include "hal/hal.h"

namespace rpc {
namespace util {

/**
 * @brief Copy a string_view into a fixed char array.
 *
 * Copies at most (dst_size - 1) characters, leaving room for the null
 * terminator already present in the zero-initialised struct.
 */
inline void copy_string(etl::string_view src, char* dst, size_t dst_size) {
  if (dst_size == 0) return;
  const size_t to_copy = etl::min(src.length(), dst_size - 1);
  etl::copy_n(src.data(), to_copy, dst);
  dst[to_copy] = rpc::RPC_NULL_TERMINATOR;
}

}  // namespace util
}  // namespace rpc

#endif
