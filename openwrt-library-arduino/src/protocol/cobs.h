#ifndef COBS_H
#define COBS_H

#include <Arduino.h>
#include <stddef.h>

// CORRECCIÓN: Eliminamos los checks estrictos de __has_include porque fallan
// en algunos entornos de Arduino si la ruta no está indexada.
// El usuario debe incluir <PacketSerial.h> en su sketch.

#include <PacketSerial.h>
#include <Encoding/COBS.h>

namespace cobs {

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

}

#endif