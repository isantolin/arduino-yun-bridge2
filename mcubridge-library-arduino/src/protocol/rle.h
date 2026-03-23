#ifndef RLE_H
#define RLE_H

#include <stddef.h>
#include <stdint.h>

#include <etl/span.h>

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

size_t encode(etl::span<const uint8_t> src, etl::span<uint8_t> dst);
size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst);

constexpr size_t MIN_COMPRESS_INPUT_SIZE = 8;
constexpr size_t MIN_COMPRESS_SAVINGS = 4;

bool should_compress(etl::span<const uint8_t> src);

} // namespace rle

#endif // RLE_H
