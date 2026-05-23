#ifndef RLE_H
#define RLE_H

#include <etl/span.h>
#include <stddef.h>
#include <stdint.h>

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

size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst);

}  // namespace rle

#endif  // RLE_H
