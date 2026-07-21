#include "SPIService.h"

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
  // [SIL-2] Timeout protection for SPI
  const uint32_t start = millis();
  size_t transferred = 0U;

  for (auto& b : buffer) {
    if (millis() - start > rpc::RPC_SPI_TIMEOUT_MS) {
      SPI.endTransaction();
      return 0;  // Hardware failure (timeout)
    }
    b = SPI.transfer(b);
    ++transferred;
  }

  SPI.endTransaction();
  return transferred;
}

SPIServiceType SPIService;

#endif  // BRIDGE_ENABLE_SPI
