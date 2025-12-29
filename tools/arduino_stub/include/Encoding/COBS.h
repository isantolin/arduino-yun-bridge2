#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <vector>

class COBS {
 public:
  static size_t encode(const uint8_t* buffer, size_t size, uint8_t* encoded) {
    if (!buffer || !encoded) return 0;
    const uint8_t* src_end = buffer + size;
    uint8_t* dst = encoded;
    uint8_t* code_ptr = dst++;
    uint8_t code = 1;

    while (buffer < src_end) {
      if (*buffer == 0) {
        *code_ptr = code;
        code_ptr = dst++;
        code = 1;
      } else {
        *dst++ = *buffer;
        code++;
        if (code == 0xFF) {
          *code_ptr = code;
          if (buffer + 1 < src_end) {
            code_ptr = dst++;
            code = 1;
          }
        }
      }
      ++buffer;
    }
    *code_ptr = code;
    return static_cast<size_t>(dst - encoded);
  }

  static size_t decode(const uint8_t* encoded, size_t size, uint8_t* decoded) {
    if (!encoded || !decoded || size == 0) return 0;

    // [COMPAT FIX] Strip trailing zero delimiter if included in size
    if (encoded[size - 1] == 0) {
        size--;
    }
    if (size == 0) return 0;

    // [SAFETY FIX] Handle in-place decoding
    if (encoded == decoded) {
        std::vector<uint8_t> temp(size);
        size_t decoded_len = decode(encoded, size, temp.data());
        if (decoded_len > 0) {
            memcpy(decoded, temp.data(), decoded_len);
        }
        return decoded_len;
    }

    const uint8_t* src = encoded;
    const uint8_t* src_end = encoded + size;
    uint8_t* dst = decoded;

    while (src < src_end) {
      uint8_t code = *src++;
      if (code == 0) return 0;

      size_t copy_len = static_cast<size_t>(code) - 1;
      if (src + copy_len > src_end) return 0;

      memcpy(dst, src, copy_len);
      src += copy_len;
      dst += copy_len;

      if (code < 0xFF && src < src_end) {
        *dst++ = 0;
      }
    }
    return static_cast<size_t>(dst - decoded);
  }
};
