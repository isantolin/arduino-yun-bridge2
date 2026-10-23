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
  constexpr decltype(SPI_MODE0) SPI_MODES[] = {SPI_MODE0, SPI_MODE1, SPI_MODE2,
                                               SPI_MODE3};
  const size_t mode_idx = static_cast<size_t>(config.data_mode >> 2U);
  const auto data_mode = (mode_idx < sizeof(SPI_MODES) / sizeof(SPI_MODES[0]))
                             ? SPI_MODES[mode_idx]
                             : SPI_MODE0;
  _settings =
      SPISettings(config.frequency,
                  static_cast<decltype(MSBFIRST)>(config.bit_order), data_mode);
}

size_t SPIServiceClass::transfer(etl::span<uint8_t> buffer) {
  if (!_initialized || buffer.empty()) return 0;

  SPI.beginTransaction(_settings);
  // [SIL-2] Timeout protection for SPI
  const uint32_t start = millis();
  size_t transferred = 0U;

  const bool completed = etl::all_of(buffer.begin(), buffer.end(), [&](auto& b) {
    if (millis() - start > rpc::RPC_SPI_TIMEOUT_MS) {
      return false;
    }
    b = SPI.transfer(b);
    ++transferred;
    return true;
  });

  SPI.endTransaction();
  return completed ? transferred : 0;
}

SPIServiceType SPIService;

#endif  // BRIDGE_ENABLE_SPI
