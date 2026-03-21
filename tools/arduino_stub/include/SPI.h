#ifndef ARDUINO_STUB_SPI_H
#define ARDUINO_STUB_SPI_H

#include <stdint.h>
#include <stddef.h>

#define LSBFIRST 0
#define MSBFIRST 1
#define SPI_MODE0 0x00
#define SPI_MODE1 0x04
#define SPI_MODE2 0x08
#define SPI_MODE3 0x0C

struct SPISettings {
    SPISettings(uint32_t freq, uint8_t bitOrder, uint8_t dataMode) {
        (void)freq; (void)bitOrder; (void)dataMode;
    }
    SPISettings() {}
};

class SPIClass {
public:
    void begin() {}
    void end() {}
    void beginTransaction(SPISettings settings) { (void)settings; }
    void endTransaction() {}
    uint8_t transfer(uint8_t data) { (void)data; return 0; }
};

extern SPIClass SPI;

#endif
