#ifndef RLE_H
#define RLE_H

#include <stddef.h>
#include <stdint.h>

#include "etl/algorithm.h"
#include "etl/span.h"
#include "etl/iterator.h"

/**
 * RLE (Run-Length Encoding) implementation for MCU Bridge protocol.
 * [SIL-2] Refactored to use ETL algorithms and pure iterators.
 */
namespace rle {

constexpr uint8_t ESCAPE_BYTE = 0xFF;
constexpr size_t MIN_RUN_LENGTH = 4;
constexpr size_t MAX_RUN_LENGTH = 256;

constexpr size_t max_encoded_size(size_t src_len) {
  return src_len * 3;
}

inline size_t encode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
  if (src.empty() || dst.empty()) return 0;

  auto src_it = src.begin();
  auto dst_it = dst.begin();

  while (src_it != src.end()) {
    uint8_t current = *src_it;
    auto run_end = etl::find_if_not(src_it + 1, etl::min(src_it + MAX_RUN_LENGTH, src.end()),
                                    [current](uint8_t b) { return b == current; });
    size_t run_len = etl::distance(src_it, run_end);

    if (run_len >= MIN_RUN_LENGTH) {
      if (etl::distance(dst_it, dst.end()) < 3) return 0;
      *dst_it++ = ESCAPE_BYTE;
      *dst_it++ = static_cast<uint8_t>(run_len - 2);
      *dst_it++ = current;
      src_it = run_end;
    } else if (current == ESCAPE_BYTE) {
      if (etl::distance(dst_it, dst.end()) < 3) return 0;
      *dst_it++ = ESCAPE_BYTE;
      *dst_it++ = (run_len == 1) ? 255 : static_cast<uint8_t>(run_len - 2);
      *dst_it++ = ESCAPE_BYTE;
      src_it = run_end;
    } else {
      if (dst_it == dst.end()) return 0;
      *dst_it++ = current;
      ++src_it;
    }
  }
  return etl::distance(dst.begin(), dst_it);
}

inline size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
  if (src.empty() || dst.empty()) return 0;

  auto src_it = src.begin();
  auto dst_it = dst.begin();

  while (src_it != src.end()) {
    uint8_t current = *src_it++;
    if (current == ESCAPE_BYTE) {
      if (etl::distance(src_it, src.end()) < 2) return 0;
      uint8_t count_m2 = *src_it++;
      uint8_t val = *src_it++;
      size_t len = (count_m2 == 255) ? 1 : static_cast<size_t>(count_m2) + 2;
      if (static_cast<size_t>(etl::distance(dst_it, dst.end())) < len) return 0;
      etl::fill_n(dst_it, len, val);
      dst_it += len;
    } else {
      if (dst_it == dst.end()) return 0;
      *dst_it++ = current;
    }
  }
  return etl::distance(dst.begin(), dst_it);
}

constexpr size_t MIN_COMPRESS_INPUT_SIZE = 8;
constexpr size_t MIN_COMPRESS_SAVINGS = 4;

inline bool should_compress(etl::span<const uint8_t> src) {
  if (src.size() < MIN_COMPRESS_INPUT_SIZE) return false;
  size_t savings = 0;
  size_t escapes = 0;
  auto it = src.begin();
  while (it != src.end()) {
    uint8_t current = *it;
    if (current == ESCAPE_BYTE) {
      escapes++;
      ++it;
      continue;
    }
    auto run_end = etl::find_if_not(it + 1, src.end(), [current](uint8_t b) { return b == current; });
    size_t len = etl::distance(it, run_end);
    if (len >= MIN_RUN_LENGTH) savings += (len - 3);
    it = run_end;
  }
  return savings > (escapes * 2 + MIN_COMPRESS_SAVINGS);
}

} // namespace rle

#endif // RLE_H
