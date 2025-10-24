#ifndef COBS_H
#define COBS_H

#include <Arduino.h>
#include <stddef.h>

namespace cobs {

/**
 * @brief COBS encodes a source buffer into a destination buffer.
 *
 * @param src_buf Pointer to the source buffer to encode.
 * @param src_len Number of bytes in the source buffer.
 * @param dst_buf Pointer to the destination buffer.
 * @return size_t The number of bytes written to the destination buffer. Does
 * NOT include the trailing zero.
 */
inline size_t encode(const uint8_t* src_buf, size_t src_len, uint8_t* dst_buf) {
  const uint8_t* src_end = src_buf + src_len;
  uint8_t* dst_start = dst_buf;
  uint8_t* code_ptr = dst_buf++;
  uint8_t code = 1;

  while (src_buf < src_end) {
    if (*src_buf == 0) {
      *code_ptr = code;
      code_ptr = dst_buf++;
      code = 1;
    } else {
      *dst_buf++ = *src_buf;
      code++;
      if (code == 0xFF) {
        *code_ptr = code;
        if (src_buf + 1 < src_end) {
          code_ptr = dst_buf++;
          code = 1;
        }
      }
    }
    src_buf++;
  }

  *code_ptr = code;
  return dst_buf - dst_start;
}

/**
 * @brief Decodes a COBS-encoded source buffer into a destination buffer.
 *
 * @param src_buf Pointer to the COBS-encoded source buffer (without the
 * trailing zero).
 * @param src_len Number of bytes in the source buffer.
 * @param dst_buf Pointer to the destination buffer.
 * @return size_t The number of bytes written to the destination buffer, or 0 on
 * decoding error.
 */
inline size_t decode(const uint8_t* src_buf, size_t src_len, uint8_t* dst_buf) {
  const uint8_t* src_end = src_buf + src_len;
  uint8_t* dst_start = dst_buf;

  while (src_buf < src_end) {
    uint8_t code = *src_buf++;
    if (code == 0) return 0;  // Should not happen in a valid packet

    size_t copy_len = code - 1;
    if (src_buf + copy_len > src_end) return 0;  // Not enough data

    memcpy(dst_buf, src_buf, copy_len);
    src_buf += copy_len;
    dst_buf += copy_len;

    if (code < 0xFF && src_buf < src_end) {
      *dst_buf++ = 0;
    }
  }
  return dst_buf - dst_start;
}

}  // namespace cobs

#endif  // COBS_H
