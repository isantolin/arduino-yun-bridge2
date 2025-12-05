#include "crc.h"

// CORRECCIÓN: Eliminamos los checks estrictos de __has_include.
// El usuario debe incluir <CRC32.h> en su sketch.

#include <CRC32.h>

uint32_t crc32_ieee(const uint8_t* data, size_t len) {
  if (!data && len > 0) {
    return 0;
  }
  return CRC32::calculate<uint8_t>(data, len);
}