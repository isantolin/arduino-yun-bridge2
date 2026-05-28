/**
 * @file pb_utils.h
 * @brief Nanopb marshalling utilities for the Arduino MCU Bridge.
 */

#ifndef BRIDGE_PB_UTILS_H
#define BRIDGE_PB_UTILS_H

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <pb.h>

namespace bridge::utils {

/**
 * @brief Safely copy a string-like source into a fixed-size Nanopb char array.
 *
 * Ensures null-termination and prevents buffer overflows.
 *
 * @tparam N Size of the destination array
 * @param src Source string view
 * @param dest Destination char array
 */
template <size_t N>
void pb_copy_string(etl::string_view src, char (&dest)[N]) {
  static_assert(N > 0, "Destination array must have size > 0");
  const size_t src_size = src.size();
  const size_t to_copy = etl::min(src_size, N - 1U);
  if (to_copy > 0U) {
    etl::copy_n(src.begin(), to_copy, dest);
  }
  dest[to_copy] = '\0';
}

/**
 * @brief Safely copy a string-like source into a fixed-size etl::array.
 *
 * @tparam N Size of the destination array
 * @param src Source string view
 * @param dest Destination etl::array
 */
template <size_t N>
void pb_copy_string(etl::string_view src, etl::array<char, N>& dest) {
  static_assert(N > 0, "Destination array must have size > 0");
  const size_t src_size = src.size();
  const size_t to_copy = etl::min(src_size, N - 1U);
  if (to_copy > 0U) {
    etl::copy_n(src.begin(), to_copy, dest.begin());
  }
  dest[to_copy] = '\0';
}

/**
 * @brief Safely copy a buffer-like source into a Nanopb bytes structure.
 *
 * @tparam DestType The Nanopb bytes structure type (must have .bytes and .size)
 * @tparam T The element type of the source span
 * @param src Source span of data
 * @param dest Destination Nanopb structure
 */
template <typename DestType, typename T>
void pb_copy_bytes(etl::span<T> src, DestType& dest) {
  const size_t src_size = src.size();
  const size_t dest_capacity = sizeof(dest.bytes);
  const size_t to_copy = etl::min(src_size, dest_capacity);
  dest.size = static_cast<pb_size_t>(to_copy);
  if (to_copy > 0U) {
    etl::copy_n(src.data(), to_copy, dest.bytes);
  }
}

}  // namespace bridge::utils

#endif  // BRIDGE_PB_UTILS_H
