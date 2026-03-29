#include "SPIService.h"
#include "Bridge.h"

#if BRIDGE_ENABLE_SPI

/* [SIL-2] Exclude from host tests if SPI stub is not linked */
#if !defined(BRIDGE_HOST_TEST)

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
  // [SIL-2] Timeout protection for SPI
  uint32_t start = bridge::now_ms();
  for (size_t i = 0; i < len; ++i) {
    if (bridge::now_ms() - start > rpc::RPC_SPI_TIMEOUT_MS) {
      // Hardware failure
      SPI.endTransaction();
      return 0; // The caller should ideally handle this
    }
    buffer[i] = SPI.transfer(buffer[i]);
  }
  SPI.endTransaction();
  return len;
}

#else

/* Mock for host tests */
SPIServiceClass::SPIServiceClass() : _initialized(false) {}
void SPIServiceClass::begin() { _initialized = true; }
void SPIServiceClass::end() { _initialized = false; }
void SPIServiceClass::setConfig(uint32_t, uint8_t, uint8_t) {}
size_t SPIServiceClass::transfer(uint8_t*, size_t len) { return len; }

#endif /* BRIDGE_HOST_TEST */

#endif // BRIDGE_ENABLE_SPI
