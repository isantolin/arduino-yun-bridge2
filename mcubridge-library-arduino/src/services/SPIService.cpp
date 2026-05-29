#include "SPIService.h"

#if BRIDGE_ENABLE_SPI

#if defined(ARDUINO_ARCH_AVR) || defined(BRIDGE_HOST_TEST)
bridge::service::SPIServiceClass<SPIClass> SPIService(SPI);
#endif

#endif // BRIDGE_ENABLE_SPI
