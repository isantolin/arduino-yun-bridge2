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

void SPIServiceClass::setConfig(const rpc::payload::SpiConfig& config) {
  _settings = SPISettings(config.frequency, config.bit_order, config.data_mode);
}

size_t SPIServiceClass::transfer(etl::span<uint8_t> buffer) {
  if (!_initialized) return 0;
  if (buffer.empty()) return 0;

  SPI.beginTransaction(_settings);
  // [SIL-2] Timeout protection for SPI
  uint32_t start = bridge::now_ms();
  auto timeout_it = etl::find_if(buffer.begin(), buffer.end(), [&](uint8_t& b) {
    if (bridge::now_ms() - start > rpc::RPC_SPI_TIMEOUT_MS) {
      return true; // Hardware failure (timeout)
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

#else

/* Mock for host tests */
SPIServiceClass::SPIServiceClass() : _initialized(false) {}
void SPIServiceClass::begin() { _initialized = true; }
void SPIServiceClass::end() { _initialized = false; }
void SPIServiceClass::setConfig(const rpc::payload::SpiConfig&) {}
size_t SPIServiceClass::transfer(etl::span<uint8_t> buffer) { return buffer.size(); }

#endif /* BRIDGE_HOST_TEST */

SPIServiceClass SPIService;

#endif // BRIDGE_ENABLE_SPI
