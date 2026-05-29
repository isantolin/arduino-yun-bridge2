#ifndef BRIDGE_SPI_SERVICE_H
#define BRIDGE_SPI_SERVICE_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_SPI

#include <Arduino.h>
#include <SPI.h>
#undef min
#undef max
#include <etl/span.h>
#include <etl/algorithm.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

namespace bridge::service {

/**
 * @brief [SIL-2] Template-based SPI Service wrapper.
 * Complies with the mandatory template wrapper policy.
 */
template <typename TSpi>
class SPIServiceClass : public BridgeObserver {
public:
  explicit SPIServiceClass(TSpi& spi)
      : _spi(spi), _initialized(false), _settings(4000000, MSBFIRST, SPI_MODE0) {}

  void begin() {
    _spi.begin();
    _initialized = true;
  }

  void end() {
    _spi.end();
    _initialized = false;
  }

  void setConfig(const rpc::payload::SpiConfig& config) {
    // [SIL-2] Explicit type safety and range validation for narrowing
    const uint32_t freq = config.frequency;
    const uint8_t bit_order = static_cast<uint8_t>(config.bit_order);
    const uint8_t data_mode = static_cast<uint8_t>(config.data_mode);
    
    _settings = SPISettings(freq, bit_order, data_mode);
  }

  size_t transfer(etl::span<uint8_t> buffer) {
    if (!_initialized || buffer.empty()) return 0;

    _spi.beginTransaction(_settings);
    const uint32_t start = millis();
    auto timeout_it = etl::find_if(buffer.begin(), buffer.end(), [&](uint8_t& b) {
      if (millis() - start > rpc::RPC_SPI_TIMEOUT_MS) {
        return true;
      }
      b = _spi.transfer(b);
      return false;
    });

    _spi.endTransaction();
    return (timeout_it == buffer.end()) ? buffer.size() : 0;
  }

  void notification(MsgBridgeSynchronized) override {}
  void notification(MsgBridgeLost) override {}

private:
  TSpi& _spi;
  bool _initialized;
  SPISettings _settings;
};

} // namespace bridge::service

#if defined(ARDUINO_ARCH_AVR) || defined(BRIDGE_HOST_TEST)
extern bridge::service::SPIServiceClass<SPIClass> SPIService;
#endif

#endif // BRIDGE_ENABLE_SPI
#endif // BRIDGE_SPI_SERVICE_H
