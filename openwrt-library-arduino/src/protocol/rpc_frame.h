#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

#include "cobs.h"
#include "crc.h"

namespace rpc {

// --- Endianness-safe helpers for Big Endian (Network Byte Order) ---

// Reads a uint16_t from a Big Endian buffer.
inline uint16_t read_u16_be(const uint8_t* buffer) {
  return ((uint16_t)buffer[0] << 8) | (uint16_t)buffer[1];
}

// Writes a uint16_t to a Big Endian buffer.
inline void write_u16_be(uint8_t* buffer, uint16_t value) {
  buffer[0] = (value >> 8) & 0xFF;
  buffer[1] = value & 0xFF;
}

constexpr uint8_t PROTOCOL_VERSION = 0x02;
constexpr size_t MAX_PAYLOAD_SIZE = 256;

// Define FrameHeader struct before it is used in sizeof()
// CRITICAL: This attribute is essential for protocol compatibility.
// It ensures the compiler creates a 5-byte struct (1 + 2 + 2) by preventing
// it from adding padding bytes for memory alignment. The Python side of the
// bridge expects a 5-byte header, and removing this attribute will break
// the communication protocol.
struct FrameHeader {
  uint8_t version;
  uint16_t payload_length;
  uint16_t command_id;
} __attribute__((packed));

static_assert(sizeof(FrameHeader) == 5, "FrameHeader must be exactly 5 bytes");

// Maximum size of a raw frame (Header + Payload + CRC)
constexpr size_t MAX_RAW_FRAME_SIZE =
    sizeof(FrameHeader) + MAX_PAYLOAD_SIZE + sizeof(uint16_t);

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
  // Consumes a byte. If a full packet is received (ending in 0x00),
  // it decodes, validates, and populates out_frame, returning true.
  bool consume(uint8_t byte, Frame& out_frame);
  void reset();

 private:
  uint8_t _rx_buffer[COBS_BUFFER_SIZE];
  size_t _rx_buffer_ptr;
};

class FrameBuilder {
 public:
  FrameBuilder();
  // Builds a raw frame into a buffer. Returns the length of the raw frame.
  size_t build(uint8_t* buffer, uint16_t command_id, const uint8_t* payload,
               uint16_t payload_len);
};

}  // namespace rpc

#endif  // RPC_FRAME_H
