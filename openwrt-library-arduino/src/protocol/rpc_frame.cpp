/**
 * @file rpc_frame.cpp
 * @brief RPC frame encoding/decoding for Arduino-Linux communication.
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements the binary framing layer with safety guarantees:
 * 
 * 1. COBS ENCODING: Consistent Overhead Byte Stuffing ensures no 0x00
 *    bytes appear in encoded data, allowing 0x00 as frame delimiter.
 * 
 * 2. CRC32 INTEGRITY: IEEE 802.3 CRC32 computed over header+payload,
 *    verified before any frame processing.
 * 
 * 3. BUFFER SAFETY: All operations use bounded arrays with explicit
 *    size checks. No heap allocation.
 * 
 * 4. IN-PLACE DECODING: COBS decode operates in-place since decoded
 *    size <= encoded size, eliminating need for temporary buffers.
 * 
 * 5. SECURE WIPE: Buffers are zeroed after use to prevent data leakage.
 * 
 * Frame format on wire:
 *   [COBS-encoded(Header + Payload + CRC32)] [0x00 delimiter]
 * 
 * Header format (5 bytes, big-endian):
 *   - version (1 byte): Protocol version (must match PROTOCOL_VERSION)
 *   - payload_length (2 bytes): Length of payload in bytes
 *   - command_id (2 bytes): Command or status code
 * 
 * @see rpc_protocol.h for protocol constants
 * @see tools/protocol/spec.toml for specification source
 */
#include "rpc_frame.h"
#include "rpc_protocol.h"
#include <FastCRC.h>

#include <string.h>

namespace rpc {

// Static FastCRC32 instance.
static FastCRC32 CRC32;

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
    if (code < RPC_UINT8_MASK && has_more) {
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

FrameParser::FrameParser() : _last_error(Error::NONE) {
  reset();
  memset(_rx_buffer, 0, sizeof(_rx_buffer));
}

void FrameParser::reset() {
  _rx_buffer_ptr = 0;
  _overflow_detected = false;
  // [SECURITY] Secure Wipe: Limpiar residuos de frames anteriores para evitar data leakage
  // en caso de errores de puntero. Costo mínimo en RAM, ganancia alta en seguridad.
  memset(_rx_buffer, 0, sizeof(_rx_buffer));
}

bool FrameParser::overflowed() const { return _overflow_detected; }

bool FrameParser::consume(uint8_t byte, Frame& out_frame) {
  // If we receive a zero byte, the packet is complete.
  if (byte == RPC_FRAME_DELIMITER) {
    if (_rx_buffer_ptr == 0) {
      return false;  // Empty packet, ignore.
    }

    // [OPTIMIZATION] In-place decoding (10/10 RAM Efficiency).
    // We decode directly into _rx_buffer because COBS decoded size <= encoded size.
    
    // [HARDENING] Security Critical: Pre-validation of COBS structure.
    size_t decoded_len = 0;
    
    // Check 1: Validate COBS structure
    if (!is_cobs_decoded_length_valid(_rx_buffer, _rx_buffer_ptr, decoded_len)) {
      reset();
      _last_error = Error::MALFORMED;
      return false; 
    }

    // Check 2: Bounds Safety Assertion
    if (decoded_len > MAX_RAW_FRAME_SIZE) {
        reset();
        _last_error = Error::MALFORMED; 
        return false;
    }

    // Check 3: Safe Decoding
    size_t actual_written = cobs::decode(_rx_buffer, _rx_buffer_ptr, _rx_buffer, sizeof(_rx_buffer));
    
    // Check 4: Integrity Verification
    if (actual_written != decoded_len) {
        reset();
        _last_error = Error::MALFORMED;
        return false;
    }

    // [FIX CRITICAL] Do NOT call reset() here. It now does a secure wipe (memset 0),
    // which would destroy the data we just decoded before we can check the CRC.
    // We manually reset the pointers to prepare logically for the next frame.
    _rx_buffer_ptr = 0;
    _overflow_detected = false;

    if (decoded_len == 0 || decoded_len > MAX_RAW_FRAME_SIZE) {
      reset(); // Now we can wipe safely as we are erroring out
      _last_error = Error::MALFORMED;
      return false;
    }

    // --- Validate CRC ---
    // The last 4 bytes of the decoded buffer (now in _rx_buffer) are the CRC32.
    if (decoded_len < CRC_TRAILER_SIZE) {
      reset(); // Security Wipe
      _last_error = Error::MALFORMED;
      return false;  // Not even enough data for a CRC.
    }
    size_t crc_start = decoded_len - CRC_TRAILER_SIZE;
    uint32_t received_crc = read_u32_be(&_rx_buffer[crc_start]);
    uint32_t calculated_crc = CRC32.crc32(_rx_buffer, crc_start);

    if (received_crc != calculated_crc) {
      reset(); // Security Wipe
      _last_error = Error::CRC_MISMATCH;
      return false;  // CRC mismatch.
    }

    // --- Extract Header ---
    size_t data_len = crc_start;  // Length of data part (header + payload)
    if (data_len < sizeof(FrameHeader)) {
      reset(); // Security Wipe
      _last_error = Error::MALFORMED;
      return false;  // Not enough data for a header.
    }
    
    // Read header fields manually
    const uint8_t* p = _rx_buffer;
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
      reset(); // Security Wipe
      _last_error = Error::MALFORMED;
      return false;
    }

    // --- Extract Payload ---
    if (out_frame.header.payload_length > 0) {
      const uint8_t* payload_src = _rx_buffer + sizeof(FrameHeader);
      memcpy(out_frame.payload, payload_src, out_frame.header.payload_length);
    }

    // [SECURITY] Secure Wipe: Now that we have extracted the data, we wipe the buffer
    // to prevent any residual data from staying in RAM longer than necessary.
    memset(_rx_buffer, 0, sizeof(_rx_buffer));

    return true;  // Successfully parsed a frame.

  } else {
    // Not a zero byte, so add it to the buffer if there's space.
    if (_rx_buffer_ptr < COBS_BUFFER_SIZE) {
      _rx_buffer[_rx_buffer_ptr++] = byte;
    } else {
      _overflow_detected = true;
      _last_error = Error::OVERFLOW;
    }
  }

  return false;
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
