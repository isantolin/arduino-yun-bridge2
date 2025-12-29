#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Minimal stub of the PacketSerial COBS helper so host-side builds can
// link against YunBridge without installing the actual Arduino library.
class COBS {
 public:
  static size_t encode(const uint8_t* buffer, size_t size, uint8_t* encoded) {
    if (!buffer || !encoded) {
      return 0;
    }
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
    if (!encoded || !decoded) {
      return 0;
    }
    const uint8_t* src = encoded;
    const uint8_t* src_end = encoded + size;
    uint8_t* dst = decoded;

    while (src < src_end) {
      uint8_t code = *src++;
      if (code == 0) {
        return 0;
      }

      size_t copy_len = static_cast<size_t>(code) - 1;
      if (src + copy_len > src_end) {
        return 0;
      }

      // FIX: Use memmove instead of memcpy to handle overlapping buffers (in-place decoding)
      memmove(dst, src, copy_len);
      src += copy_len;
      dst += copy_len;

      if (code < 0xFF && src < src_end) {
        *dst++ = 0;
      }
    }

    return static_cast<size_t>(dst - decoded);
  }
};#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Minimal stub of the PacketSerial COBS helper so host-side builds can
// link against YunBridge without installing the actual Arduino library.
class COBS {
 public:
  static size_t encode(const uint8_t* buffer, size_t size, uint8_t* encoded) {
    if (!buffer || !encoded) {
      return 0;
    }
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
    if (!encoded || !decoded) {
      return 0;
    }
    const uint8_t* src = encoded;
    const uint8_t* src_end = encoded + size;
    uint8_t* dst = decoded;

    while (src < src_end) {
      uint8_t code = *src++;
      if (code == 0) {
        return 0;
      }

      size_t copy_len = static_cast<size_t>(code) - 1;
      if (src + copy_len > src_end) {
        return 0;
      }

      // FIX: Use memmove instead of memcpy to handle overlapping buffers (in-place decoding)
      memmove(dst, src, copy_len);
      src += copy_len;
      dst += copy_len;

      if (code < 0xFF && src < src_end) {
        *dst++ = 0;
      }
    }

    return static_cast<size_t>(dst - decoded);
  }
};
