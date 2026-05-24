#include "services/SPIService.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_SPI

SPIServiceClass::SPIServiceClass() {}

void SPIServiceClass::begin() {}
void SPIServiceClass::end() {}

size_t SPIServiceClass::transfer(etl::span<uint8_t> data) {
  (void)data;
  return 0;
}

void SPIServiceClass::setConfig(const rpc_pb_SpiConfig& config) {
  (void)config;
}

SPIServiceClass SPIService;

#endif
