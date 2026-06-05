#include "SPIService.h"

#include "Bridge.h"

#if BRIDGE_ENABLE_SPI

/* [SIL-2] SPI implementation with timeout protection */

template <typename T>
SPIServiceClass<T>::SPIServiceClass()
    : _initialized(false), _settings(4000000, MSBFIRST, SPI_MODE0) {}

template <typename T>
void SPIServiceClass<T>::begin() {
  SPI.begin();
  _initialized = true;
}

template <typename T>
void SPIServiceClass<T>::end() {
  SPI.end();
  _initialized = false;
}

template <typename T>
void SPIServiceClass<T>::setConfig(const rpc::payload::SpiConfig& config) {
  _settings = SPISettings(config.frequency, config.bit_order,
                          config.data_mode);
}

template <typename T>
size_t SPIServiceClass<T>::transfer(etl::span<uint8_t> buffer) {
  if (!_initialized) return 0;
  if (buffer.empty()) return 0;

  SPI.beginTransaction(_settings);
  // [SIL-2] Timeout protection for SPI
  uint32_t start = millis();
  auto timeout_it = etl::find_if(buffer.begin(), buffer.end(), [&](uint8_t& b) {
    if (millis() - start > rpc::RPC_SPI_TIMEOUT_MS) {
      return true;  // Hardware failure (timeout)
    }
    b = SPI.transfer(b);
    return false;
  });

  if (timeout_it != buffer.end()) {
    SPI.endTransaction();
    return 0;
  }
  SPI.endTransaction();
  return buffer.size();
}

template class SPIServiceClass<void>;
SPIServiceType SPIService;

#endif  // BRIDGE_ENABLE_SPI
