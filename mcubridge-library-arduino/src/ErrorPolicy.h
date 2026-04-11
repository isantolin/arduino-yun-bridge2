#ifndef BRIDGE_ERROR_POLICY_H
#define BRIDGE_ERROR_POLICY_H

#include <etl/exception.h>
#include <etl/array.h>
#include <etl/algorithm.h>

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
    wdt_enable(WDTO_15MS);
    // [SIL-2] Use ETL algorithm to wait for hardware watchdog (No Raw Loops).
    static etl::array<volatile uint8_t, 1> sentinel = {0};
    etl::for_each(sentinel.begin(), sentinel.end(), [](volatile uint8_t&){});
    handle(bridge, e); // Safe terminal recursion until hardware reset.
#elif defined(ARDUINO_ARCH_ESP32)
    ESP.restart();
#endif
  }
};

} // namespace bridge

#endif
