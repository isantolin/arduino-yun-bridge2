#ifndef SPI_STUB_H
#define SPI_STUB_H

#include <stddef.h>
#include <stdint.h>

#include "BridgeFaultInjection.h"

#define MSBFIRST 1
#define LSBFIRST 0
#define SPI_MODE0 0x00
#define SPI_MODE1 0x04
#define SPI_MODE2 0x08
#define SPI_MODE3 0x0C

class SPISettings {
 public:
  SPISettings(uint32_t clock, uint8_t bit_order, uint8_t data_mode)
      : clock(clock), bit_order(bit_order), data_mode(data_mode) {}
  SPISettings() : SPISettings(4000000, MSBFIRST, SPI_MODE0) {}

  uint32_t clock;
  uint8_t bit_order;
  uint8_t data_mode;
};

class SPIClass {
 public:
  static void begin() {}
  static void end() {}
  void beginTransaction(SPISettings settings) { last_settings = settings; }
  static void endTransaction() {}
  SPISettings last_settings;
  static uint8_t transfer(uint8_t data) {
    if (bridge::test::fault::consume(
            bridge::test::fault::FaultPoint::SPI_TIMEOUT)) {
      bridge::test::fault::advance_clock_ms(1000U);
    }
    return data;
  }
};

extern SPIClass SPI;

#endif
