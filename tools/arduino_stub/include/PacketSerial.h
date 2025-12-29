#pragma once
#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <vector>
#include <functional>
#include <Encoding/COBS.h>

class PacketSerial {
 public:
  using PacketHandler = void (*)(const uint8_t* buffer, size_t size);
  PacketSerial() : stream_(nullptr), handler_(nullptr) { buffer_.reserve(256); }
  void setStream(Stream* stream) { stream_ = stream; }
  void setPacketHandler(PacketHandler handler) { handler_ = handler; }
  void update() {
    if (!stream_) return;
    while (stream_->available() > 0) {
      int byte = stream_->read();
      if (byte < 0) break;
      uint8_t data = static_cast<uint8_t>(byte);
      if (data == 0) {
        if (!buffer_.empty() && handler_) {
            size_t decoded_len = COBS::decode(buffer_.data(), buffer_.size(), buffer_.data());
            if (decoded_len > 0) handler_(buffer_.data(), decoded_len);
        }
        buffer_.clear();
      } else {
        buffer_.push_back(data);
      }
    }
  }
  size_t send(const uint8_t* buffer, size_t len) {
    if (!stream_ || !buffer || len == 0) return 0;
    std::vector<uint8_t> encoded(len + len / 254 + 2);
    size_t encoded_len = COBS::encode(buffer, len, encoded.data());
    size_t written = stream_->write(encoded.data(), encoded_len);
    stream_->write(0);
    return written;
  }
 private:
  Stream* stream_;
  PacketHandler handler_;
  std::vector<uint8_t> buffer_;
};
