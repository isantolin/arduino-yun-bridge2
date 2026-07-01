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
  void setConfig(const rpc::payload::SpiConfig& config);
  size_t transfer(etl::span<uint8_t> buffer);

  void onLost() { end(); }

 private:
  bool _initialized;
  SPISettings _settings;
};

using SPIServiceType = SPIServiceClass;
extern SPIServiceType SPIService;

#endif  // BRIDGE_ENABLE_SPI
#endif  // BRIDGE_SPI_SERVICE_H
