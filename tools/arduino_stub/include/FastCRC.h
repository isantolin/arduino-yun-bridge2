#ifndef FASTCRC_H
#define FASTCRC_H

#include <stddef.h>
#include <stdint.h>

// Mock for FastCRC32 library for host-side testing
class FastCRC32 {
public:
    uint32_t crc32(const uint8_t *data, uint16_t len) {
        // Standard IEEE 802.3 CRC32 implementation
        // Matches the behavior of the hardware/optimized FastCRC library
        uint32_t crc = 0xFFFFFFFF;
        for (uint16_t i = 0; i < len; i++) {
            crc ^= data[i];
            for (uint8_t j = 0; j < 8; j++) {
                if (crc & 1)
                    crc = (crc >> 1) ^ 0xEDB88320;
                else
                    crc >>= 1;
            }
        }
        return ~crc;
    }
};

#endif
