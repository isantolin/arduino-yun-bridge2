#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

#include "cobs.h"
#include "crc.h"
#include "rpc_protocol.h"

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
// CRITICAL: This attribute is essential for protocol compatibility.
struct FrameHeader {
  uint8_t version;
  uint16_t payload_length;
  uint16_t command_id;
} __attribute__((packed));

static_assert(sizeof(FrameHeader) == 5, "FrameHeader must be exactly 5 bytes");

// Maximum size of a raw frame (Header + Payload + CRC)
constexpr size_t MAX_RAW_FRAME_SIZE =
  sizeof(FrameHeader) + MAX_PAYLOAD_SIZE + CRC_TRAILER_SIZE;

// Buffer to hold a COBS-encoded frame. Overhead is 1 byte per 254-byte block +
// 1 code byte. Add a little extra for safety.
constexpr size_t COBS_BUFFER_SIZE =
    MAX_RAW_FRAME_SIZE + (MAX_RAW_FRAME_SIZE / 254) + 2;

struct Frame {
  FrameHeader header;
  uint8_t payload[MAX_PAYLOAD_SIZE];
};

class FrameParser {
 public:
  FrameParser();
  // Consumes a byte. If a full packet is received (ending in RPC_FRAME_DELIMITER),
  // it decodes, validates, and populates out_frame, returning true.
  bool consume(uint8_t byte, Frame& out_frame);
  void reset();
  bool overflowed() const;
  
  enum class Error {
    NONE,
    CRC_MISMATCH,
    MALFORMED,
    OVERFLOW
  };
  Error getError() const { return _last_error; }
  void clearError() { _last_error = Error::NONE; }

 private:
  uint8_t _rx_buffer[COBS_BUFFER_SIZE];
  size_t _rx_buffer_ptr;
  bool _overflow_detected;
  Error _last_error;
};

class FrameBuilder {
 public:
  FrameBuilder();
  // Builds a raw frame into a buffer. Returns the length of the raw frame.
  // SAFETY: Now requires the buffer size to prevent overflows.
  size_t build(uint8_t* buffer,
               size_t buffer_size,
               uint16_t command_id,
               const uint8_t* payload,
               size_t payload_len);
};

}  // namespace rpc

#endif  // RPC_FRAME_H