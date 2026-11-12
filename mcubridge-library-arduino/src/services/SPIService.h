#ifndef BRIDGE_SPI_SERVICE_H
#define BRIDGE_SPI_SERVICE_H

#include "Bridge.h"

#if BRIDGE_ENABLE_SPI

#include <SPI.h>
#undef min
#undef max
#include <pw_span/span.h>

#include "protocol/mcu_wire.pb.h"

class SPIServiceClass : public bridge::BridgeObserver {
 public:
  SPIServiceClass();

  void begin();
  void end();
  void setConfig(const rpc_pb_SpiConfig& config);
  size_t transfer(pw::span<uint8_t> buffer);

  void onLost() override { end(); }

 private:
  bool _initialized;
  SPISettings _settings;
};

using SPIServiceType = SPIServiceClass;
extern SPIServiceType SPIService;

#endif  // BRIDGE_ENABLE_SPI
#endif  // BRIDGE_SPI_SERVICE_H
