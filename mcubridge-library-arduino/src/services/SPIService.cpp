#include "SPIService.h"

#if BRIDGE_ENABLE_SPI

SPIServiceClass::SPIServiceClass() 
  : _initialized(false), _settings(4000000, MSBFIRST, SPI_MODE0) {}

void SPIServiceClass::begin() {
  SPI.begin();
  _initialized = true;
}

void SPIServiceClass::end() {
  SPI.end();
  _initialized = false;
}

void SPIServiceClass::setConfig(uint32_t frequency, uint8_t bitOrder, uint8_t dataMode) {
  _settings = SPISettings(frequency, bitOrder, dataMode);
}

size_t SPIServiceClass::transfer(uint8_t* buffer, size_t len) {
  if (!_initialized) return 0;
  if (len == 0) return 0;

  SPI.beginTransaction(_settings);
  for (size_t i = 0; i < len; ++i) {
    buffer[i] = SPI.transfer(buffer[i]);
  }
  SPI.endTransaction();
  return len;
}

#endif // BRIDGE_ENABLE_SPI
