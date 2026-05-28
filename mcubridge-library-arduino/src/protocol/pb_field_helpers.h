#pragma once

#include <stddef.h>
#include <stdint.h>

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include <etl/string_view.h>

#include "pb.h"

namespace rpc::pb_field {

template <size_t N>
inline size_t copy_string_view_trunc(etl::string_view src,
                                     char (&dst)[N]) noexcept {
  static_assert(N > 0U, "Destination buffer must have room for a terminator");
  const size_t to_copy = etl::min(src.size(), N - 1U);
  if (to_copy > 0U) {
    etl::copy_n(src.begin(), to_copy, dst);
  }
  dst[to_copy] = '\0';
  return to_copy;
}

template <size_t N>
inline size_t copy_string_view_trunc(etl::string_view src,
                                     etl::array<char, N>& dst) noexcept {
  static_assert(N > 0U, "Destination buffer must have room for a terminator");
  const size_t to_copy = etl::min(src.size(), N - 1U);
  if (to_copy > 0U) {
    etl::copy_n(src.begin(), to_copy, dst.begin());
  }
  dst[to_copy] = '\0';
  return to_copy;
}

template <typename PbBytesField>
inline size_t copy_span_to_bytes_field(etl::span<const uint8_t> src,
                                       PbBytesField& dst) noexcept {
  const size_t capacity = sizeof(dst.bytes) / sizeof(dst.bytes[0]);
  const size_t max_count = etl::min(capacity, static_cast<size_t>(PB_SIZE_MAX));
  const size_t to_copy = etl::min(src.size(), max_count);
  dst.size = static_cast<pb_size_t>(to_copy);
  if (to_copy > 0U) {
    etl::copy_n(src.data(), to_copy, dst.bytes);
  }
  return to_copy;
}

template <typename PbBytesField>
inline etl::span<const uint8_t> bytes_field_as_span(
    const PbBytesField& src) noexcept {
  return etl::span<const uint8_t>(src.bytes, static_cast<size_t>(src.size));
}

}  // namespace rpc::pb_field
