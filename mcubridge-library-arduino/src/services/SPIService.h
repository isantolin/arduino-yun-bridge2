#ifndef BRIDGE_SPI_SERVICE_H
#define BRIDGE_SPI_SERVICE_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_SPI

#include <SPI.h>
#undef min
#undef max
#include <etl/span.h>
#include "protocol/rpc_structs.h"

class SPIServiceClass {
public:
  SPIServiceClass();

  void begin();
  void end();
  void setConfig(uint32_t frequency, uint8_t bitOrder, uint8_t dataMode);
  size_t transfer(etl::span<uint8_t> buffer);

  bool isInitialized() const { return _initialized; }

private:
  bool _initialized;
  SPISettings _settings;
};

extern SPIServiceClass SPIService;

#endif // BRIDGE_ENABLE_SPI
#endif // BRIDGE_SPI_SERVICE_H
