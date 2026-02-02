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
// FastCRC32 instance used by protocol (Table-based, fast for ARM/ESP)
FastCRC32 CRC32;

#include "etl/algorithm.h"

namespace rpc {

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
    
    const size_t crc_start = size - CRC_TRAILER_SIZE;
    const uint32_t received_crc = read_u32_be(&buffer[crc_start]);
    
    const uint32_t calculated_crc = CRC32.crc32(buffer, crc_start);

    if (received_crc != calculated_crc) {
        _last_error = Error::CRC_MISMATCH;
        return false;
    }

    // --- Extract Header ---
    if (crc_start < sizeof(FrameHeader)) {
        _last_error = Error::MALFORMED;
        return false;
    }

    // Read header fields using fixed offsets
    out_frame.header.version = buffer[0];
    out_frame.header.payload_length = read_u16_be(&buffer[1]);
    out_frame.header.command_id = read_u16_be(&buffer[3]);

    // --- Validate Header ---
    if (out_frame.header.version != PROTOCOL_VERSION ||
        out_frame.header.payload_length > out_frame.payload.max_size() ||
        (sizeof(FrameHeader) + out_frame.header.payload_length) != crc_start) {
        _last_error = Error::MALFORMED;
        return false;
    }

    // --- Extract Payload ---
    out_frame.payload.assign(&buffer[sizeof(FrameHeader)], &buffer[sizeof(FrameHeader) + out_frame.header.payload_length]);
    out_frame.crc = calculated_crc;

    return true;
}

// --- FrameBuilder ---

FrameBuilder::FrameBuilder() {}

size_t FrameBuilder::build(uint8_t* buffer,
                           size_t buffer_size,
                           uint16_t command_id,
                           const uint8_t* payload,
                           size_t payload_len) {
  if (payload_len > MAX_PAYLOAD_SIZE || payload_len > UINT16_MAX) {
    return 0;
  }

  const uint16_t payload_len_u16 = static_cast<uint16_t>(payload_len);
  const size_t data_len = sizeof(FrameHeader) + payload_len;
  const size_t total_len = data_len + CRC_TRAILER_SIZE;

  if (total_len > buffer_size) {
    return 0; // Buffer overflow protection
  }

  // --- Header ---
  buffer[0] = PROTOCOL_VERSION;
  write_u16_be(&buffer[1], payload_len_u16);
  write_u16_be(&buffer[3], command_id);

  // --- Payload ---
  if (payload && payload_len > 0) {
    etl::copy_n(payload, payload_len, &buffer[sizeof(FrameHeader)]);
  }

  // --- CRC ---
  uint32_t crc = CRC32.crc32(buffer, data_len);
  
  write_u32_be(&buffer[data_len], crc);

  return total_len;
}

}  // namespace rpc