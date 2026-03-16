/**
 * @file hal.h
 * @brief Hardware Abstraction Layer for Arduino MCU Bridge v2.
 */
#ifndef BRIDGE_HAL_H
#define BRIDGE_HAL_H

#include <Arduino.h>
#include <stdint.h>

namespace bridge {
namespace hal {

/**
 * @brief Get the amount of free RAM available.
 * @return Free bytes or UINT16_MAX if detection fails.
 */
uint16_t getFreeMemory();

/**
 * @brief Validate if a pin number is valid for the current board.
 * @param pin The pin number to validate.
 * @return true if valid, false otherwise.
 */
bool isValidPin(uint8_t pin);

/**
 * @brief Initialize hardware specific features (e.g. Watchdog).
 */
void init();

}  // namespace hal
}  // namespace bridge

// [SIL-2] Atomic Block Abstraction
#if defined(ARDUINO_ARCH_AVR)
#include <util/atomic.h>
#define BRIDGE_ATOMIC_BLOCK ATOMIC_BLOCK(ATOMIC_RESTORESTATE)
#else
struct BridgeAtomicGuard {
  BridgeAtomicGuard() {
    noInterrupts();
    asm volatile("" ::: "memory");
  }
  ~BridgeAtomicGuard() {
    asm volatile("" ::: "memory");
    interrupts();
  }
};
#define BRIDGE_ATOMIC_BLOCK                                     \
  for (int _guard_active = 1; _guard_active; _guard_active = 0) \
    for (BridgeAtomicGuard _guard; _guard_active; _guard_active = 0)
#endif

// --- PROGMEM portability shim (centralized) ---
#include "progmem_compat.h"

#endif  // BRIDGE_HAL_H
