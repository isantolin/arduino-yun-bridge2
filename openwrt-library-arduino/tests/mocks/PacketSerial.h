#ifndef MOCK_PACKET_SERIAL_H
#define MOCK_PACKET_SERIAL_H

#include <stddef.h>
#include <stdint.h>

class PacketSerial {
 public:
  PacketSerial() = default;
  template <typename T>
  void setStream(T*) {}
  template <typename T>
  void setPacketHandler(T) {}
  void update() {}
  size_t send(const uint8_t*, size_t len) { return len; }
};

#endif
