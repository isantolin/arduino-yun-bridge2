#ifndef SPI_STUB_H
#define SPI_STUB_H

#include <stddef.h>
#include <stdint.h>

#define MSBFIRST 1
#define LSBFIRST 0
#define SPI_MODE0 0x00

class SPISettings {
 public:
  SPISettings(uint32_t clock, uint8_t bitOrder, uint8_t dataMode) {}
  SPISettings() {}
};

class SPIClass {
 public:
  void begin() {}
  void end() {}
  void beginTransaction(SPISettings settings) {}
  void endTransaction() {}
  uint8_t transfer(uint8_t data) { return data; }
};

extern SPIClass SPI;

#endif
