#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>
#include "rpc_protocol.h"

// ETL requires min/max from <algorithm>, but Arduino.h defines them as macros.
#undef min
#undef max
#include "etl/array.h"
#include "etl/expected.h"

namespace rpc {

// --- Endianness-safe helpers for Big Endian (Network Byte Order) ---

// Reads a uint16_t from a Big Endian buffer.
inline uint16_t read_u16_be(const uint8_t* buffer) {
  return ((uint16_t)buffer[0] << 8) | (uint16_t)buffer[1];
}

// Writes a uint16_t to a Big Endian buffer.
inline void write_u16_be(uint8_t* buffer, uint16_t value) {
  buffer[0] = (value >> 8) & RPC_UINT8_MASK;
  buffer[1] = value & RPC_UINT8_MASK;
}

// Reads a uint32_t from a Big Endian buffer.
inline uint32_t read_u32_be(const uint8_t* buffer) {
  return (static_cast<uint32_t>(buffer[0]) << 24) |
         (static_cast<uint32_t>(buffer[1]) << 16) |
         (static_cast<uint32_t>(buffer[2]) << 8) |
         static_cast<uint32_t>(buffer[3]);
}

// Writes a uint32_t to a Big Endian buffer.
inline void write_u32_be(uint8_t* buffer, uint32_t value) {
  buffer[0] = static_cast<uint8_t>((value >> 24) & RPC_UINT8_MASK);
  buffer[1] = static_cast<uint8_t>((value >> 16) & RPC_UINT8_MASK);
  buffer[2] = static_cast<uint8_t>((value >> 8) & RPC_UINT8_MASK);
  buffer[3] = static_cast<uint8_t>(value & RPC_UINT8_MASK);
}

constexpr size_t CRC_TRAILER_SIZE = sizeof(uint32_t);

// Define FrameHeader struct before it is used in sizeof()
struct FrameHeader {
  uint8_t version;
  uint16_t payload_length;
  uint16_t command_id;
} __attribute__((packed));

static_assert(sizeof(FrameHeader) == 5, "FrameHeader must be exactly 5 bytes");

// Maximum size of a raw frame (Header + Payload + CRC)
constexpr size_t MAX_RAW_FRAME_SIZE =
  sizeof(FrameHeader) + MAX_PAYLOAD_SIZE + CRC_TRAILER_SIZE;

struct Frame {
  FrameHeader header;
  etl::array<uint8_t, MAX_PAYLOAD_SIZE> payload;
  uint32_t crc;
};

/**
 * @brief Parse error codes for FrameParser.
 * [SIL-2] Explicit error types enable type-safe error handling.
 */
enum class FrameError {
  CRC_MISMATCH,   ///< CRC32 validation failed
  MALFORMED,      ///< Frame structure invalid (size, version, lengths)
  OVERFLOW        ///< Payload exceeds maximum allowed size
};

class FrameParser {
 public:
  FrameParser() = default;
  
  /**
   * @brief Parse a decoded frame buffer.
   * 
   * [SIL-2 COMPLIANT] Uses etl::expected for type-safe error handling.
   * Returns either a valid Frame or an error code, eliminating the
   * bool + out_param pattern that can lead to use-after-failure bugs.
   * 
   * @param buffer Decoded frame data (post-COBS)
   * @param size Size of buffer in bytes
   * @return etl::expected<Frame, FrameError> - Frame on success, error on failure
   */
  etl::expected<Frame, FrameError> parse(const uint8_t* buffer, size_t size);
  
  // Legacy compatibility aliases (deprecated, will be removed)
  using Error = FrameError;
  static constexpr FrameError Error_NONE = static_cast<FrameError>(-1); // Sentinel for legacy code
  static constexpr FrameError Error_CRC_MISMATCH = FrameError::CRC_MISMATCH;
  static constexpr FrameError Error_MALFORMED = FrameError::MALFORMED;
  static constexpr FrameError Error_OVERFLOW = FrameError::OVERFLOW;
};

class FrameBuilder {
 public:
  FrameBuilder();
  // Builds a raw frame into a buffer. Returns the length of the raw frame.
  size_t build(uint8_t* buffer,
               size_t buffer_size,
               uint16_t command_id,
               const uint8_t* payload,
               size_t payload_len);
};

}  // namespace rpc

#endif  // RPC_FRAME_H
