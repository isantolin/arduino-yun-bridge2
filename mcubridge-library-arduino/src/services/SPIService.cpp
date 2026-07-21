#include "SPIService.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_SPI

/* [SIL-2] SPI implementation with timeout protection */

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

void SPIServiceClass::setConfig(const rpc::payload::SpiConfig& config) {
  _settings = SPISettings(config.frequency, config.bit_order, config.data_mode);
}

size_t SPIServiceClass::transfer(etl::span<uint8_t> buffer) {
  if (!_initialized || buffer.empty()) return 0;

  SPI.beginTransaction(_settings);
  const uint32_t start = millis();
  size_t transferred = 0U;

  etl::for_each(buffer.begin(), buffer.end(), [&](uint8_t& b) {
    if (millis() - start <= rpc::RPC_SPI_TIMEOUT_MS) {
      b = SPI.transfer(b);
      ++transferred;
    }
  });

  SPI.endTransaction();
  return (transferred == buffer.size()) ? transferred : 0;
}

SPIServiceType SPIService;

#endif  // BRIDGE_ENABLE_SPI
