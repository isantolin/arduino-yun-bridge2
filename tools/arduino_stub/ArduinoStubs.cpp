#include "SPI.h"
#include "Arduino.h"
#include "BridgeFaultInjection.h"

SPIClass SPI;
HardwareSerial Serial __attribute__((weak));
HardwareSerial Serial1 __attribute__((weak));

Stream* g_arduino_stream_delegate __attribute__((weak)) = nullptr;

#ifdef ARDUINO_STUB_CUSTOM_MILLIS
unsigned long millis() __attribute__((weak));
unsigned long millis() {
    return bridge::test::fault::clock_ms();
}

void delay(unsigned long ms) __attribute__((weak));
void delay(unsigned long ms) {
    bridge::test::fault::advance_clock_ms(static_cast<uint32_t>(ms));
}
#endif

#include <etl/exception.h>
namespace etl {
void handle_error(const etl::exception&) {}
}
