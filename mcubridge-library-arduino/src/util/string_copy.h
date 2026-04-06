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

/**
 * @brief Join parts (command + args) into a single null-terminated string.
 */
inline void copy_join(etl::string_view base, etl::span<const etl::string_view> parts, char* dst, size_t dst_size) {
  if (dst_size == 0) return;

  char* current = dst;
  char* const end = dst + dst_size - 1; // Reserve room for null terminator

  // Copy base string
  const size_t base_len = etl::min(base.length(), static_cast<size_t>(end - current));
  current = etl::copy_n(base.data(), base_len, current);

  // Join parts with space separator
  for (const auto& part : parts) {
    if (current >= end) break;
    
    // Add separator if there is room for it and at least one character of the part
    *current++ = ' ';
    
    const size_t part_len = etl::min(part.length(), static_cast<size_t>(end - current));
    current = etl::copy_n(part.data(), part_len, current);
  }

  *current = rpc::RPC_NULL_TERMINATOR;
}

// Legacy aliases during migration
inline void pb_copy_string(etl::string_view src, char* dst, size_t dst_size) {
  copy_string(src, dst, dst_size);
}

inline void pb_copy_join(etl::string_view base, etl::span<const etl::string_view> parts, char* dst, size_t dst_size) {
  copy_join(base, parts, dst, dst_size);
}

} // namespace util
} // namespace rpc

#endif
