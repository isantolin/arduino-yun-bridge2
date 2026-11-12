#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include "SPIService.h"
#include <pw_span/span.h>
#if defined(ARDUINO_ARCH_AVR)
#include <avr/pgmspace.h>
#endif
#if BRIDGE_ENABLE_SPI
namespace {
#if defined(ARDUINO_ARCH_AVR)
const uint8_t SPI_MODES[] PROGMEM = {SPI_MODE0, SPI_MODE1, SPI_MODE2, SPI_MODE3};
uint8_t readSpiMode(size_t i) { return pgm_read_byte(&SPI_MODES[i]); }
#else
constexpr uint8_t SPI_MODES[] = {SPI_MODE0, SPI_MODE1, SPI_MODE2, SPI_MODE3};
uint8_t readSpiMode(size_t i) { return SPI_MODES[i]; }
#endif
}
SPIServiceClass::SPIServiceClass() : _initialized(false), _settings(4000000, MSBFIRST, SPI_MODE0) {}
void SPIServiceClass::begin() { SPI.begin(); _initialized = true; }
void SPIServiceClass::end() { SPI.end(); _initialized = false; }
void SPIServiceClass::setConfig(const rpc_pb_SpiConfig& config) {
  const size_t mode_idx = static_cast<size_t>(config.data_mode >> 2U);
  const auto data_mode = mode_idx < sizeof(SPI_MODES) / sizeof(SPI_MODES[0]) ? readSpiMode(mode_idx) : SPI_MODE0;
  _settings = SPISettings(config.frequency,
      config.bit_order == rpc_pb_SpiBitOrder_SPI_BIT_ORDER_LSB_FIRST ? LSBFIRST : MSBFIRST,
      data_mode);
}
size_t SPIServiceClass::transfer(pw::span<uint8_t> buffer) {
  if (!_initialized || buffer.empty()) return 0;
  SPI.beginTransaction(_settings);
  const uint32_t start = millis();
  size_t transferred = 0U;
  const bool completed = etl::all_of(buffer.begin(), buffer.end(), [&](auto& b) {
    if (millis() - start > bridge::config::SPI_TIMEOUT_MS) return false;
    b = SPI.transfer(b); ++transferred; return true;
  });
  SPI.endTransaction();
  return completed ? transferred : 0;
}
SPIServiceType SPIService;
#endif
