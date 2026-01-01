#pragma once
#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <Encoding/COBS.h>

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
          size_t decoded_len = COBS::decode(buffer_, buffer_len_, buffer_);
          if (decoded_len > 0) {
            handler_(buffer_, decoded_len);
          }
        }
        buffer_len_ = 0;
      } else {
        if (buffer_len_ < kBufferCapacity) {
          buffer_[buffer_len_++] = data;
        } else {
          // Drop overflowed packet.
          buffer_len_ = 0;
        }
      }
    }
  }
  size_t send(const uint8_t* buffer, size_t len) {
    if (!stream_ || !buffer || len == 0) return 0;

    const size_t encoded_capacity = len + len / 254 + 2;
    uint8_t* encoded = new uint8_t[encoded_capacity];
    size_t written = 0;
    if (encoded) {
      size_t encoded_len = COBS::encode(buffer, len, encoded);
      written = stream_->write(encoded, encoded_len);
      delete[] encoded;
    }
    stream_->write(static_cast<uint8_t>(0));
    return written;
  }
 private:
  Stream* stream_;
  PacketHandler handler_;
  uint8_t buffer_[kBufferCapacity] = {};
  size_t buffer_len_;
};
