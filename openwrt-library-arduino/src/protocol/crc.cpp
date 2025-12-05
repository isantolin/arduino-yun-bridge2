#include "crc.h"

#if defined(__has_include)
#if __has_include(<CRC32.h>)
#define BRIDGE_HAS_PACKET_CRC32 1
#endif
#endif

#if !defined(BRIDGE_HAS_PACKET_CRC32)
#error "CRC32 dependency missing: install bakercp/CRC32 so <CRC32.h> is available."
#endif

#include <CRC32.h>

uint32_t crc32_ieee(const uint8_t* data, size_t len) {
  if (!data && len > 0) {
    return 0;
  }
  return CRC32::calculate<uint8_t>(data, len);
}
