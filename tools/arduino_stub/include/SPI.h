#ifndef SPI_STUB_H
#define SPI_STUB_H

#include "BridgeFaultInjection.h"

#include <stdint.h>
#include <stddef.h>

#define MSBFIRST 1
#define LSBFIRST 0
#define SPI_MODE0 0x00

class SPISettings {
public:
    SPISettings(uint32_t, uint8_t, uint8_t) {}
    SPISettings() {}
};

class SPIClass {
public:
    void begin() {}
    void end() {}
    void beginTransaction(SPISettings) {}
    void endTransaction() {}
    uint8_t transfer(uint8_t data) {
        if (bridge::test::fault::consume(
                bridge::test::fault::FaultPoint::SPI_TIMEOUT)) {
            bridge::test::fault::advance_clock_ms(1000U);
        }
        return data;
    }
};

extern SPIClass SPI;

#endif
