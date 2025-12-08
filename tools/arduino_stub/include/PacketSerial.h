#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

#include <Encoding/COBS.h>

// Lightweight PacketSerial stub so host builds can include <PacketSerial.h>
// without pulling the full Arduino dependency tree.
class PacketSerial {
 public:
  PacketSerial() = default;

  template <typename T>
  void setStream(T*) {}

  template <typename T>
  void setPacketHandler(T) {}

  void update() {}

  size_t send(const uint8_t* buffer, size_t len) {
    return buffer && len ? len : 0;
  }
};
