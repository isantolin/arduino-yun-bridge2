#ifndef BRIDGE_SPI_SERVICE_H
#define BRIDGE_SPI_SERVICE_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_SPI

#include <SPI.h>
#undef min
#undef max
#include <etl/span.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

class SPIServiceClass : public BridgeObserver {
public:
  SPIServiceClass();

  void begin();
  void end();
  void setConfig(const rpc::payload::SpiConfig& config);
  size_t transfer(etl::span<uint8_t> buffer);

  void notification(MsgBridgeSynchronized) override {}
  void notification(MsgBridgeLost) override {}

private:
  bool _initialized;
  SPISettings _settings;
};

extern SPIServiceClass SPIService;

#endif // BRIDGE_ENABLE_SPI
#endif // BRIDGE_SPI_SERVICE_H
