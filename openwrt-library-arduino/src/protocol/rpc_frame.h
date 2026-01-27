#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>
#include "rpc_protocol.h"

// ETL requires min/max from <algorithm>, but Arduino.h defines them as macros.
#undef min
#undef max
#include "etl/array.h"

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
};

class FrameParser {
 public:
  FrameParser();
  
  // Parses a DECODED buffer (already stripped of COBS by PacketSerial).
  // Validates CRC and Protocol rules.
  bool parse(const uint8_t* buffer, size_t size, Frame& out_frame);
  
  enum class Error {
    NONE,
    CRC_MISMATCH,
    MALFORMED,
    OVERFLOW
  };
  Error getError() const { return _last_error; }
  void clearError() { _last_error = Error::NONE; }

 private:
  Error _last_error;
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
