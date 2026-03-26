#ifndef RLE_H
#define RLE_H

#include <stddef.h>
#include <stdint.h>

#include <etl/span.h>
#include "rpc_protocol.h"

/**
 * RLE (Run-Length Encoding) implementation for MCU Bridge protocol.
 * [SIL-2] Refactored to use ETL algorithms and pure iterators.
 */
namespace rle {

constexpr uint8_t ESCAPE_BYTE = rpc::RPC_RLE_ESCAPE_BYTE;
constexpr size_t MIN_RUN_LENGTH = rpc::RPC_RLE_MIN_RUN_LENGTH;
constexpr size_t MAX_RUN_LENGTH = rpc::RPC_RLE_MAX_RUN_LENGTH;
constexpr uint8_t SINGLE_ESCAPE_MARKER = rpc::RPC_RLE_SINGLE_ESCAPE_MARKER;

constexpr size_t max_encoded_size(size_t src_len) {
  return src_len * 3;
}

size_t encode(etl::span<const uint8_t> src, etl::span<uint8_t> dst);
size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst);

constexpr size_t MIN_COMPRESS_INPUT_SIZE = rpc::RPC_RLE_MIN_COMPRESS_INPUT_SIZE;
constexpr size_t MIN_COMPRESS_SAVINGS = rpc::RPC_RLE_MIN_COMPRESS_SAVINGS;

bool should_compress(etl::span<const uint8_t> src);

} // namespace rle

#endif // RLE_H
