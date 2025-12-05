#ifndef CRC_H
#define CRC_H

#include <stddef.h>
#include <stdint.h>

// Computes a CRC32 (IEEE 802.3 polynomial) over the provided buffer.
uint32_t crc32_ieee(const uint8_t* data, size_t len);

#endif  // CRC_H
