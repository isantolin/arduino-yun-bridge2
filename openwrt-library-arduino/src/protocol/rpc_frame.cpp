#include "rpc_frame.h"

#include <string.h>

namespace rpc {

namespace {

bool is_cobs_decoded_length_valid(const uint8_t* encoded,
                                  size_t encoded_len,
                                  size_t& decoded_len) {
  decoded_len = 0;
  size_t index = 0;

  while (index < encoded_len) {
    uint8_t code = encoded[index++];
    if (code == 0) {
      return false;
    }

    if (decoded_len + static_cast<size_t>(code) - 1 > MAX_RAW_FRAME_SIZE) {
      return false;
    }
    decoded_len += static_cast<size_t>(code) - 1;

    if (index + static_cast<size_t>(code) - 1 > encoded_len) {
      return false;  // Not enough encoded bytes for the claimed data segment.
    }
    index += static_cast<size_t>(code) - 1;

    const bool has_more = index < encoded_len;
    if (code < 0xFF && has_more) {
      if (decoded_len >= MAX_RAW_FRAME_SIZE) {
        return false;
      }
      decoded_len += 1;  // account for inserted zero byte
    }
  }

  return decoded_len <= MAX_RAW_FRAME_SIZE;
}

}  // namespace

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
    size_t decoded_len = 0;
    if (!is_cobs_decoded_length_valid(_rx_buffer, _rx_buffer_ptr,
                                      decoded_len)) {
      reset();
      return false;  // Would overflow destination buffer.
    }

    decoded_len = cobs::decode(_rx_buffer, _rx_buffer_ptr, decoded_buffer);

    reset();  // Reset for the next packet.

    if (decoded_len == 0 || decoded_len > MAX_RAW_FRAME_SIZE) {
      return false;  // COBS decoding failed or produced oversize frame.
    }

    // --- Validate CRC ---
    // The last 4 bytes of the decoded buffer are the CRC32.
    if (decoded_len < CRC_TRAILER_SIZE) {
      return false;  // Not even enough data for a CRC.
    }
    size_t crc_start = decoded_len - CRC_TRAILER_SIZE;
    uint32_t received_crc = read_u32_be(&decoded_buffer[crc_start]);
    uint32_t calculated_crc = crc32_ieee(decoded_buffer, crc_start);

    if (received_crc != calculated_crc) {
      return false;  // CRC mismatch.
    }

    // --- Extract Header ---
    size_t data_len = crc_start;  // Length of data part (header + payload)
    if (data_len < sizeof(FrameHeader)) {
      return false;  // Not enough data for a header.
    }
    
    // Read header fields manually to ensure correct endianness
    const uint8_t* p = decoded_buffer;
    out_frame.header.version = *p++;
    out_frame.header.payload_length = read_u16_be(p);
    p += 2;
    out_frame.header.command_id = read_u16_be(p);
    p += 2;


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
  // Write header fields manually to ensure correct Big Endian byte order
  uint8_t* p = buffer;
  *p++ = PROTOCOL_VERSION;
  write_u16_be(p, payload_len);
  p += 2;
  write_u16_be(p, command_id);
  p += 2;

  // Copy payload into the buffer
  if (payload && payload_len > 0) {
    memcpy(p, payload, payload_len);
  }

  size_t data_len = sizeof(FrameHeader) + payload_len;

  // --- CRC ---
  uint32_t crc = crc32_ieee(buffer, data_len);
  write_u32_be(buffer + data_len, crc);

  return data_len + CRC_TRAILER_SIZE;  // Return total raw frame length
}

}  // namespace rpc
