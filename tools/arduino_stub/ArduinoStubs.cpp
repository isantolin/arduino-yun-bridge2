#include "SPI.h"
#include "Arduino.h"

SPIClass SPI;

#ifdef ARDUINO_STUB_CUSTOM_MILLIS
void delay(unsigned long ms) __attribute__((weak));
void delay(unsigned long ms) {
    (void)ms;
}
#endif

#include <etl/exception.h>
namespace etl {
void handle_error(const etl::exception& e) {
  (void)e;
}
}
