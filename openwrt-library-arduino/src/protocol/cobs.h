#ifndef COBS_H
#define COBS_H

#include <stddef.h>
#include <stdint.h>

/**
 * @file cobs.h
 * @brief Consistent Overhead Byte Stuffing (COBS) encoding/decoding.
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * Native implementation replacing PacketSerial dependency for:
 * - Better control over buffer handling
 * - Explicit size validation
 * - No hidden allocations
 * 
 * COBS Properties:
 * - Guaranteed no 0x00 bytes in output (allows 0x00 as delimiter)
 * - Worst-case overhead: 1 byte per 254 input bytes + 1 code byte
 * - Decoded size always <= encoded size (enables in-place decoding)
 * 
 * @see https://en.wikipedia.org/wiki/Consistent_Overhead_Byte_Stuffing
 */
namespace cobs {

/**
 * Encode data using COBS.
 * 
 * @param src_buf  Source buffer containing raw data (may contain zeros)
 * @param src_len  Length of source data
 * @param dst_buf  Destination buffer for encoded data (must be at least src_len + src_len/254 + 1 bytes)
 * @return         Length of encoded data, or 0 on error
 * 
 * The worst-case encoded size is: src_len + (src_len / 254) + 1
 */
inline size_t encode(const uint8_t* src_buf, size_t src_len, uint8_t* dst_buf) {
  if (!src_buf || !dst_buf || src_len == 0) {
    return 0;
  }

  const uint8_t* src = src_buf;
  const uint8_t* src_end = src_buf + src_len;
  uint8_t* dst = dst_buf;
  uint8_t* code_ptr = dst++;  // Pointer to the current code byte
  uint8_t code = 1;           // Current code value (distance to next zero or end)

  while (src < src_end) {
    if (*src == 0) {
      // Found a zero byte - write the current code and start new block
      *code_ptr = code;
      code_ptr = dst++;
      code = 1;
      src++;
    } else {
      // Non-zero byte - copy it and increment code
      *dst++ = *src++;
      code++;

      // If we've reached 255 bytes without a zero, we need to insert a code byte
      if (code == 0xFF) {
        *code_ptr = code;
        code_ptr = dst++;
        code = 1;
      }
    }
  }

  // Write the final code byte
  *code_ptr = code;

  return static_cast<size_t>(dst - dst_buf);
}

/**
 * Decode COBS-encoded data.
 * 
 * @param src_buf  Source buffer containing COBS-encoded data
 * @param src_len  Length of encoded data
 * @param dst_buf  Destination buffer for decoded data (can be same as src_buf for in-place decode)
 * @return         Length of decoded data, or 0 on error
 * 
 * Note: Decoded data is always smaller than or equal to encoded data,
 * so in-place decoding is safe.
 */
inline size_t decode(const uint8_t* src_buf, size_t src_len, uint8_t* dst_buf) {
  if (!src_buf || !dst_buf || src_len == 0) {
    return 0;
  }

  const uint8_t* src = src_buf;
  const uint8_t* src_end = src_buf + src_len;
  uint8_t* dst = dst_buf;

  while (src < src_end) {
    uint8_t code = *src++;

    if (code == 0) {
      // Unexpected zero in encoded data - malformed
      return 0;
    }

    // Copy (code - 1) data bytes
    for (uint8_t i = 1; i < code && src < src_end; i++) {
      *dst++ = *src++;
    }

    // If code < 0xFF, there's an implicit zero (unless we're at the end)
    if (code < 0xFF && src < src_end) {
      *dst++ = 0;
    }
  }

  return static_cast<size_t>(dst - dst_buf);
}

}  // namespace cobs

#endif  // COBS_H