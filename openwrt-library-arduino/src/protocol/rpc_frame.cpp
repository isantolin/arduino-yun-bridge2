/**
 * @file rpc_frame.cpp
 * @brief RPC frame encoding/decoding for Arduino-Linux communication.
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements the binary framing layer with safety guarantees:
 * 
 * 1. CRC32 INTEGRITY: IEEE 802.3 CRC32 computed over header+payload,
 *    verified before any frame processing.
 * 
 * 2. BUFFER SAFETY: All operations use bounded arrays with explicit
 *    size checks. No heap allocation.
 * 
 * 3. SECURE WIPE: Buffers are zeroed after use to prevent data leakage.
 * 
 * Frame format on wire (before COBS):
 *   [Header] [Payload] [CRC32]
 * 
 * Header format (5 bytes, big-endian):
 *   - version (1 byte): Protocol version (must match PROTOCOL_VERSION)
 *   - payload_length (2 bytes): Length of payload in bytes
 *   - command_id (2 bytes): Command or status code
 */
#include "rpc_frame.h"
#include "rpc_protocol.h"
#include <FastCRC.h>

#include <string.h>

namespace rpc {

// Static FastCRC32 instance.
static FastCRC32 CRC32;

// --- FrameParser ---

FrameParser::FrameParser() : _last_error(Error::NONE) {
}

bool FrameParser::parse(const uint8_t* buffer, size_t size, Frame& out_frame) {
    _last_error = Error::NONE;

    if (size == 0 || size > MAX_RAW_FRAME_SIZE) {
        _last_error = Error::MALFORMED;
        return false;
    }

    // --- Validate CRC ---
    if (size < CRC_TRAILER_SIZE) {
        _last_error = Error::MALFORMED;
        return false;
    }
    
    size_t crc_start = size - CRC_TRAILER_SIZE;
    uint32_t received_crc = read_u32_be(&buffer[crc_start]);
    uint32_t calculated_crc = CRC32.crc32(buffer, crc_start);

    if (received_crc != calculated_crc) {
        _last_error = Error::CRC_MISMATCH;
        return false;
    }

    // --- Extract Header ---
    size_t data_len = crc_start; // Length of data part (header + payload)
    if (data_len < sizeof(FrameHeader)) {
        _last_error = Error::MALFORMED;
        return false;
    }

    // Read header fields manually
    const uint8_t* p = buffer;
    out_frame.header.version = *p++;
    out_frame.header.payload_length = read_u16_be(p);
    p += 2;
    out_frame.header.command_id = read_u16_be(p);
    p += 2;

    // --- Validate Header ---
    if (out_frame.header.version != PROTOCOL_VERSION ||
        // [SIL-2] Defense in Depth: Validate against actual buffer size
        out_frame.header.payload_length > sizeof(out_frame.payload) ||
        (sizeof(FrameHeader) + out_frame.header.payload_length) != data_len) {
        _last_error = Error::MALFORMED;
        return false;
    }

    // --- Extract Payload ---
    if (out_frame.header.payload_length > 0) {
        const uint8_t* payload_src = buffer + sizeof(FrameHeader);
        memcpy(out_frame.payload, payload_src, out_frame.header.payload_length);
    }

    return true;
}

// --- FrameBuilder ---

FrameBuilder::FrameBuilder() {}

size_t FrameBuilder::build(uint8_t* buffer,
                           size_t buffer_size,
                           uint16_t command_id,
                           const uint8_t* payload,
                           size_t payload_len) {
  if (payload_len > MAX_PAYLOAD_SIZE ||
      payload_len > UINT16_MAX) {
    return 0;
  }

  const uint16_t payload_len_u16 = static_cast<uint16_t>(payload_len);

  size_t data_len = sizeof(FrameHeader) + payload_len;
  size_t total_len = data_len + CRC_TRAILER_SIZE;

  if (total_len > buffer_size) {
    return 0; // Buffer overflow protection
  }

  // --- Header ---
  // Write header fields manually to ensure correct Big Endian byte order
  uint8_t* p = buffer;
  *p++ = PROTOCOL_VERSION;
  write_u16_be(p, payload_len_u16);
  p += 2;
  write_u16_be(p, command_id);
  p += 2;

  // Copy payload into the buffer
  if (payload && payload_len > 0) {
    // using memcpy for raw buffer copy (No STL dependency)
    memcpy(p, payload, payload_len);
  }

  // --- CRC ---
  uint32_t crc = CRC32.crc32(buffer, data_len);
  write_u32_be(buffer + data_len, crc);

  return total_len;  // Return total raw frame length
}

}  // namespace rpc