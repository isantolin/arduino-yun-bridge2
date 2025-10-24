#include "rpc_frame.h"

#include <string.h>

namespace rpc {

// --- FrameParser ---

FrameParser::FrameParser() {
  reset();
  memset(_rx_buffer, 0, sizeof(_rx_buffer));
}

void FrameParser::reset() { _rx_buffer_ptr = 0; }

bool FrameParser::consume(uint8_t byte, Frame& out_frame) {
  // If we receive a zero byte, the packet is complete.
  if (byte == 0) {
    if (_rx_buffer_ptr == 0) {
      return false;  // Empty packet, ignore.
    }

    // We have a complete COBS-encoded packet in _rx_buffer.
    uint8_t decoded_buffer[MAX_RAW_FRAME_SIZE];
    size_t decoded_len =
        cobs::decode(_rx_buffer, _rx_buffer_ptr, decoded_buffer);

    reset();  // Reset for the next packet.

    if (decoded_len == 0) {
      return false;  // COBS decoding failed.
    }

    // --- Validate CRC ---
    // The last 2 bytes of the decoded buffer are the CRC.
    if (decoded_len < sizeof(uint16_t)) {
      return false;  // Not even enough data for a CRC.
    }
    size_t crc_start = decoded_len - sizeof(uint16_t);
    uint16_t received_crc = (uint16_t)decoded_buffer[crc_start] |
                            ((uint16_t)decoded_buffer[crc_start + 1] << 8);
    uint16_t calculated_crc = crc16_ccitt(decoded_buffer, crc_start);

    if (received_crc != calculated_crc) {
      return false;  // CRC mismatch.
    }

    // --- Extract Header ---
    size_t data_len = crc_start;  // Length of data part (header + payload)
    if (data_len < sizeof(FrameHeader)) {
      return false;  // Not enough data for a header.
    }
    memcpy(&out_frame.header, decoded_buffer, sizeof(FrameHeader));

    // --- Validate Header ---
    if (out_frame.header.version != PROTOCOL_VERSION ||
        out_frame.header.payload_length > MAX_PAYLOAD_SIZE ||
        (sizeof(FrameHeader) + out_frame.header.payload_length) != data_len) {
      return false;  // Invalid version, payload length, or overall size.
    }

    // --- Extract Payload ---
    if (out_frame.header.payload_length > 0) {
      memcpy(out_frame.payload, decoded_buffer + sizeof(FrameHeader),
             out_frame.header.payload_length);
    }

    return true;  // Successfully parsed a frame.

  } else {
    // Not a zero byte, so add it to the buffer if there's space.
    if (_rx_buffer_ptr < COBS_BUFFER_SIZE) {
      _rx_buffer[_rx_buffer_ptr++] = byte;
    }
    // If the buffer overflows, the packet will be corrupt and fail COBS/CRC
    // check later.
  }

  return false;
}

// --- FrameBuilder ---

FrameBuilder::FrameBuilder() {}

size_t FrameBuilder::build(uint8_t* buffer, uint16_t command_id,
                           const uint8_t* payload, uint16_t payload_len) {
  if (payload_len > MAX_PAYLOAD_SIZE) {
    return 0;
  }

  // --- Header ---
  FrameHeader header;
  header.version = PROTOCOL_VERSION;
  header.payload_length = payload_len;
  header.command_id = command_id;

  // Copy header and payload into the buffer
  memcpy(buffer, &header, sizeof(FrameHeader));
  if (payload && payload_len > 0) {
    memcpy(buffer + sizeof(FrameHeader), payload, payload_len);
  }

  size_t data_len = sizeof(FrameHeader) + payload_len;

  // --- CRC ---
  uint16_t crc = crc16_ccitt(buffer, data_len);
  buffer[data_len] = crc & 0xFF;
  buffer[data_len + 1] = (crc >> 8) & 0xFF;

  return data_len + sizeof(uint16_t);  // Return total raw frame length
}

}  // namespace rpc
