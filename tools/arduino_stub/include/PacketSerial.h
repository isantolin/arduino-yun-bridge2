#pragma once
#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/**
 * @brief Minimal COBS stub integrated into PacketSerial for host-side tests.
 */
class PacketSerial {
 public:
  using PacketHandler = void (*)(const uint8_t* buffer, size_t size);
  static constexpr size_t kBufferCapacity = 512;

  PacketSerial() : stream_(nullptr), handler_(nullptr), buffer_len_(0) {}
  void setStream(Stream* stream) { stream_ = stream; }
  void setPacketHandler(PacketHandler handler) { handler_ = handler; }

  void update() {
    if (!stream_) return;
    while (stream_->available() > 0) {
      int byte = stream_->read();
      if (byte < 0) break;
      uint8_t data = static_cast<uint8_t>(byte);
      if (data == 0) {
        if (buffer_len_ > 0 && handler_) {
          uint8_t decoded[kBufferCapacity];
          size_t decoded_len = decode(buffer_, buffer_len_, decoded);
          if (decoded_len > 0) {
            handler_(decoded, decoded_len);
          }
        }
        buffer_len_ = 0;
      } else {
        if (buffer_len_ < kBufferCapacity) {
          buffer_[buffer_len_++] = data;
        } else {
          buffer_len_ = 0;
        }
      }
    }
  }

  size_t send(const uint8_t* buffer, size_t len) {
    if (!stream_ || !buffer || len == 0) return 0;
    static constexpr size_t kMaxEncodeLen = kBufferCapacity + kBufferCapacity / 254 + 2;
    uint8_t encoded[kMaxEncodeLen];
    size_t encoded_len = encode(buffer, len, encoded);
    size_t written = stream_->write(encoded, encoded_len);
    stream_->write(static_cast<uint8_t>(0));
    return written;
  }

 private:
  static size_t encode(const uint8_t* src, size_t len, uint8_t* dst) {
    uint8_t* code_ptr = dst++;
    uint8_t code = 1;
    for (size_t i = 0; i < len; ++i) {
      if (src[i] == 0) {
        *code_ptr = code;
        code_ptr = dst++;
        code = 1;
      } else {
        *dst++ = src[i];
        if (++code == 0xFF) {
          *code_ptr = code;
          code_ptr = dst++;
          code = 1;
        }
      }
    }
    *code_ptr = code;
    return dst - (code_ptr - (code - 1)); // Simplified calculation
  }

  static size_t decode(const uint8_t* src, size_t len, uint8_t* dst) {
    const uint8_t* end = src + len;
    uint8_t* out = dst;
    while (src < end) {
      uint8_t code = *src++;
      for (uint8_t i = 1; i < code; ++i) *out++ = *src++;
      if (code < 0xFF && src < end) *out++ = 0;
    }
    return out - dst;
  }

  Stream* stream_;
  PacketHandler handler_;
  uint8_t buffer_[kBufferCapacity];
  size_t buffer_len_;
};
