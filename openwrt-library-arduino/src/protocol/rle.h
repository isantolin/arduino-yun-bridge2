#ifndef RLE_H
#define RLE_H

#include <stddef.h>
#include <stdint.h>

/**
 * RLE (Run-Length Encoding) implementation for Yun Bridge protocol.
 * 
 * Simple compression optimized for embedded systems with minimal RAM.
 * Uses escape-based encoding to handle all byte values:
 * 
 * Format:
 *   - Literal byte (not 0xFF): output as-is
 *   - Escape sequence (0xFF): followed by count byte, then repeated byte
 *     - count 0-254: run length = count + 2 (so 2-256 bytes)
 *     - count 255: special marker meaning exactly 1 byte (for single 0xFF)
 *     
 * Examples:
 *   0xFF 0x03 0x41 = 'A' repeated 5 times (3+2)
 *   0xFF 0xFF 0xFF = single 0xFF byte (special case)
 *   0xFF 0x00 0xFF = two 0xFF bytes
 * 
 * Only encodes runs of 4+ identical bytes (break-even at 3).
 * 
 * Worst case expansion: 3x for data with many isolated 0xFF bytes.
 * Best case compression: ~85x for uniform data.
 * 
 * RAM usage: ~10 bytes stack (no heap allocation).
 */
namespace rle {

/// Escape byte used to signal a run
constexpr uint8_t ESCAPE_BYTE = 0xFF;

/// Minimum run length to encode (shorter runs are left as literals)
constexpr size_t MIN_RUN_LENGTH = 4;

/// Maximum run length in a single encoded sequence (254 + 2 = 256)
/// Note: 255 is reserved as special marker for single-byte escapes
constexpr size_t MAX_RUN_LENGTH = 256;

/**
 * Calculate maximum encoded size for given input length.
 * 
 * Worst case: every byte is 0xFF with no runs = 3 bytes per input byte.
 * In practice, this rarely happens.
 */
constexpr size_t max_encoded_size(size_t src_len) {
  return src_len * 3;  // Absolute worst case (all 0xFF, no runs)
}

/**
 * Encode data using RLE.
 * 
 * @param src_buf  Source buffer with raw data
 * @param src_len  Length of source data
 * @param dst_buf  Destination buffer (must be at least src_len bytes, 
 *                 ideally max_encoded_size(src_len) for safety)
 * @param dst_max  Maximum size of destination buffer
 * @return         Length of encoded data, or 0 on error (buffer overflow)
 */
inline size_t encode(const uint8_t* src_buf, size_t src_len, 
                     uint8_t* dst_buf, size_t dst_max) {
  if (!src_buf || !dst_buf || src_len == 0 || dst_max == 0) {
    return 0;
  }

  size_t src_pos = 0;
  size_t dst_pos = 0;

  while (src_pos < src_len) {
    uint8_t current = src_buf[src_pos];
    
    // Count consecutive identical bytes
    size_t run_len = 1;
    while (src_pos + run_len < src_len && 
           src_buf[src_pos + run_len] == current &&
           run_len < MAX_RUN_LENGTH) {
      run_len++;
    }

    if (run_len >= MIN_RUN_LENGTH) {
      // Encode as run: ESCAPE, count-2, byte
      if (dst_pos + 3 > dst_max) return 0;  // Buffer overflow
      dst_buf[dst_pos++] = ESCAPE_BYTE;
      dst_buf[dst_pos++] = static_cast<uint8_t>(run_len - 2);
      dst_buf[dst_pos++] = current;
      src_pos += run_len;
    } else if (current == ESCAPE_BYTE) {
      // Escape byte(s) but not enough for MIN_RUN_LENGTH
      // Special handling for 0xFF bytes:
      // - Single 0xFF: ESCAPE, 255, 0xFF (255 = special marker for 1)
      // - 2 0xFF: ESCAPE, 0, 0xFF
      // - 3 0xFF: ESCAPE, 1, 0xFF
      if (dst_pos + 3 > dst_max) return 0;
      dst_buf[dst_pos++] = ESCAPE_BYTE;
      if (run_len == 1) {
        dst_buf[dst_pos++] = 255;  // Special: exactly 1 byte
      } else {
        dst_buf[dst_pos++] = static_cast<uint8_t>(run_len - 2);
      }
      dst_buf[dst_pos++] = ESCAPE_BYTE;
      src_pos += run_len;
    } else {
      // Literal byte
      if (dst_pos + 1 > dst_max) return 0;
      dst_buf[dst_pos++] = current;
      src_pos++;
    }
  }

  return dst_pos;
}

/**
 * Decode RLE-encoded data.
 * 
 * @param src_buf  Source buffer with RLE-encoded data
 * @param src_len  Length of encoded data
 * @param dst_buf  Destination buffer for decoded data
 * @param dst_max  Maximum size of destination buffer
 * @return         Length of decoded data, or 0 on error (malformed or overflow)
 */
inline size_t decode(const uint8_t* src_buf, size_t src_len,
                     uint8_t* dst_buf, size_t dst_max) {
  if (!src_buf || !dst_buf || src_len == 0 || dst_max == 0) {
    return 0;
  }

  size_t src_pos = 0;
  size_t dst_pos = 0;

  while (src_pos < src_len) {
    uint8_t current = src_buf[src_pos++];

    if (current == ESCAPE_BYTE) {
      // Encoded run: need at least 2 more bytes
      if (src_pos + 2 > src_len) return 0;  // Malformed
      
      uint8_t count_minus_2 = src_buf[src_pos++];
      uint8_t byte_val = src_buf[src_pos++];
      
      // Special case: 255 means exactly 1 byte (for single 0xFF)
      size_t run_len;
      if (count_minus_2 == 255) {
        run_len = 1;
      } else {
        run_len = static_cast<size_t>(count_minus_2) + 2;
      }
      
      if (dst_pos + run_len > dst_max) return 0;  // Overflow
      
      for (size_t i = 0; i < run_len; i++) {
        dst_buf[dst_pos++] = byte_val;
      }
    } else {
      // Literal byte
      if (dst_pos + 1 > dst_max) return 0;
      dst_buf[dst_pos++] = current;
    }
  }

  return dst_pos;
}

/**
 * Check if compression would be beneficial.
 * 
 * Quick heuristic: count potential runs without full encoding.
 * Returns true if encoding is likely to save space.
 * 
 * @param src_buf  Source buffer to analyze
 * @param src_len  Length of source data
 * @return         True if compression is recommended
 */
inline bool should_compress(const uint8_t* src_buf, size_t src_len) {
  if (!src_buf || src_len < 8) return false;  // Too small to benefit
  
  size_t potential_savings = 0;
  size_t escape_count = 0;
  size_t i = 0;
  
  while (i < src_len) {
    uint8_t current = src_buf[i];
    
    if (current == ESCAPE_BYTE) {
      escape_count++;
      i++;
      continue;
    }
    
    // Count run
    size_t run_len = 1;
    while (i + run_len < src_len && src_buf[i + run_len] == current) {
      run_len++;
    }
    
    if (run_len >= MIN_RUN_LENGTH) {
      // Run of N bytes becomes 3 bytes, saving N-3 bytes
      potential_savings += run_len - 3;
    }
    
    i += run_len;
  }
  
  // Each escape byte in non-run context costs 2 extra bytes
  size_t escape_cost = escape_count * 2;
  
  return potential_savings > escape_cost + 4;  // Need meaningful savings
}

}  // namespace rle

#endif  // RLE_H
