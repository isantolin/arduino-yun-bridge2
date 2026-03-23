#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

#include "rpc_protocol.h"

// ETL requires min/max from <algorithm>, but Arduino.h defines them as macros.
#undef min
#undef max
#include "etl/array.h"
#include "etl/binary.h"
#include "etl/crc32.h"
#include "etl/expected.h"
#include "etl/span.h"

namespace rpc {

// --- Endianness-safe helpers for Big Endian (Network Byte Order) ---

// Reads a uint16_t from a Big Endian buffer.
inline uint16_t read_u16_be(etl::span<const uint8_t> buffer) {
  if (buffer.size() < 2) return 0;
  uint16_t value;
  etl::copy_n(buffer.data(), 2, reinterpret_cast<uint8_t*>(&value));
  return etl::reverse_bytes(value);
}

// Writes a uint16_t to a Big Endian buffer.
inline void write_u16_be(etl::span<uint8_t> buffer, uint16_t value) {
  if (buffer.size() < 2) return;
  uint16_t swapped = etl::reverse_bytes(value);
  etl::copy_n(reinterpret_cast<const uint8_t*>(&swapped), 2, buffer.data());
}

// Reads a uint32_t from a Big Endian buffer.
inline uint32_t read_u32_be(etl::span<const uint8_t> buffer) {
  if (buffer.size() < 4) return 0;
  uint32_t value;
  etl::copy_n(buffer.data(), 4, reinterpret_cast<uint8_t*>(&value));
  return etl::reverse_bytes(value);
}

// Writes a uint32_t to a Big Endian buffer.
inline void write_u32_be(etl::span<uint8_t> buffer, uint32_t value) {
  if (buffer.size() < 4) return;
  uint32_t swapped = etl::reverse_bytes(value);
  etl::copy_n(reinterpret_cast<const uint8_t*>(&swapped), 4, buffer.data());
}

// Reads a uint64_t from a Big Endian buffer.
inline uint64_t read_u64_be(etl::span<const uint8_t> buffer) {
  if (buffer.size() < 8) return 0;
  uint64_t value;
  etl::copy_n(buffer.data(), 8, reinterpret_cast<uint8_t*>(&value));
  return etl::reverse_bytes(value);
}

// Writes a uint64_t to a Big Endian buffer.
inline void write_u64_be(etl::span<uint8_t> buffer, uint64_t value) {
  if (buffer.size() < 8) return;
  uint64_t swapped = etl::reverse_bytes(value);
  etl::copy_n(reinterpret_cast<const uint8_t*>(&swapped), 8, buffer.data());
}

constexpr size_t CRC_TRAILER_SIZE = sizeof(uint32_t);

// --- Protocol Offset Constants [SIL-2] ---
constexpr size_t VERSION_OFFSET = 0;
constexpr size_t PAYLOAD_LENGTH_OFFSET = 1;
constexpr size_t COMMAND_ID_OFFSET = 3;
constexpr size_t SEQUENCE_ID_OFFSET = 5;
constexpr size_t FRAME_HEADER_SIZE = 7;
constexpr size_t MIN_FRAME_SIZE = FRAME_HEADER_SIZE + CRC_TRAILER_SIZE;

// Define FrameHeader struct before it is used in sizeof()
#pragma pack(push, 1)
struct FrameHeader {
  uint8_t version;
  uint16_t payload_length;
  uint16_t command_id;
  uint16_t sequence_id;
};
#pragma pack(pop)

static_assert(sizeof(FrameHeader) == 7, "FrameHeader must be exactly 7 bytes");

// Maximum size of a raw frame (Header + Payload + CRC)
constexpr size_t MAX_RAW_FRAME_SIZE =
    sizeof(FrameHeader) + MAX_PAYLOAD_SIZE + CRC_TRAILER_SIZE;

struct Frame {
  FrameHeader header;
  etl::span<const uint8_t> payload;
  uint32_t crc;
};

/**
 * @brief Parse error codes for FrameParser.
 * [SIL-2] Explicit error types enable type-safe error handling.
 */
enum class FrameError {
  CRC_MISMATCH,  ///< CRC32 validation failed
  MALFORMED,     ///< Frame structure invalid (size, version, lengths)
  OVERFLOW       ///< Payload exceeds maximum allowed size
};

class FrameParser {
 public:
  FrameParser() = default;
  etl::expected<Frame, FrameError> parse(etl::span<const uint8_t> buffer) {
    if (buffer.size() < 11 || buffer.size() > MAX_RAW_FRAME_SIZE)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);
    const size_t crc_start = buffer.size() - 4;
    const uint32_t received_crc = read_u32_be(buffer.subspan(crc_start));
    etl::crc32 crc_calc;
    crc_calc.add(buffer.data(), buffer.data() + crc_start);
    if (received_crc != crc_calc.value())
      return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);
    if (buffer[0] != PROTOCOL_VERSION)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);
    const uint16_t payload_len = read_u16_be(buffer.subspan(1));
    if (buffer.size() != (static_cast<size_t>(payload_len) + 11))
      return etl::unexpected<FrameError>(FrameError::MALFORMED);
    if (payload_len > MAX_PAYLOAD_SIZE)
      return etl::unexpected<FrameError>(FrameError::OVERFLOW);
    
    Frame result;
    result.header.version = buffer[0];
    result.header.payload_length = payload_len;
    result.header.command_id = read_u16_be(buffer.subspan(3));
    result.header.sequence_id = read_u16_be(buffer.subspan(5));
    result.payload = buffer.subspan(7, payload_len);
    result.crc = crc_calc.value();
    return result;
  }
};

class FrameBuilder {
 public:
  FrameBuilder() = default;
  size_t build(etl::span<uint8_t> buffer, uint16_t command_id, uint16_t sequence_id,
               etl::span<const uint8_t> payload) {
    if (payload.size() > MAX_PAYLOAD_SIZE) return 0;
    const uint16_t payload_len = static_cast<uint16_t>(payload.size());
    const size_t data_len = 7 + payload_len;
    const size_t total_len = data_len + 4;
    if (total_len > buffer.size()) return 0;
    etl::fill_n(buffer.begin(), data_len, 0);
    buffer[0] = PROTOCOL_VERSION;
    write_u16_be(buffer.subspan(1), payload_len);
    write_u16_be(buffer.subspan(3), command_id);
    write_u16_be(buffer.subspan(5), sequence_id);
    if (payload_len > 0)
      etl::copy_n(payload.begin(), payload_len, buffer.begin() + 7);
    etl::crc32 crc_calc;
    crc_calc.add(buffer.data(), buffer.data() + data_len);
    write_u32_be(buffer.subspan(data_len), crc_calc.value());
    return total_len;
  }
};

}  // namespace rpc

#endif  // RPC_FRAME_H
