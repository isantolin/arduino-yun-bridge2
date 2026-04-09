#ifndef BRIDGE_ERROR_POLICY_H
#define BRIDGE_ERROR_POLICY_H

#include <etl/exception.h>

namespace bridge {

struct SafeStatePolicy {
  template <typename TBridge>
  static void handle(TBridge& bridge, const etl::exception& e) {
    (void)e;
    bridge.enterSafeState();
  }
};

struct ResetPolicy {
  template <typename TBridge>
  static void handle(TBridge& bridge, const etl::exception& e) {
    (void)bridge; (void)e;
#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
    wdt_enable(WDTO_15MS); while(true);
#elif defined(ARDUINO_ARCH_ESP32)
    ESP.restart();
#endif
  }
};

} // namespace bridge

#endif
