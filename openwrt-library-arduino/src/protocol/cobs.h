#ifndef COBS_H
#define COBS_H

#include <Arduino.h>
#include <stddef.h>

#if defined(__has_include)
#if __has_include(<PacketSerial.h>)
#include <PacketSerial.h>
#define BRIDGE_HAS_PACKET_SERIAL 1
#endif
#endif

#if !defined(BRIDGE_HAS_PACKET_SERIAL)
#error "PacketSerial dependency missing: install bakercp/PacketSerial so <PacketSerial.h> is available."
#endif

#if defined(__has_include)
#if __has_include(<Encoding/COBS.h>)
#define BRIDGE_HAS_PACKET_SERIAL_COBS 1
#endif
#endif

#if !defined(BRIDGE_HAS_PACKET_SERIAL_COBS)
#error "PacketSerial installation incomplete: <Encoding/COBS.h> not found after installing bakercp/PacketSerial."
#endif

#include <Encoding/COBS.h>

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
inline size_t encode(
    const uint8_t* src_buf, size_t src_len, uint8_t* dst_buf) {
  if (!src_buf || !dst_buf) {
    return 0;
  }
  return ::COBS::encode(src_buf, src_len, dst_buf);
}

inline size_t decode(
    const uint8_t* src_buf, size_t src_len, uint8_t* dst_buf) {
  if (!src_buf || !dst_buf) {
    return 0;
  }
  return ::COBS::decode(src_buf, src_len, dst_buf);
}

}  // namespace cobs

#endif  // COBS_H
