#ifndef CRC_H
#define CRC_H

#include <stddef.h>
#include <stdint.h>

uint16_t crc16_ccitt_init();
uint16_t crc16_ccitt_update(uint16_t crc, uint8_t data);
uint16_t crc16_ccitt_update(uint16_t crc, const uint8_t *data, size_t len);
uint16_t crc16_ccitt(const uint8_t *data, size_t len);

#endif  // CRC_H
