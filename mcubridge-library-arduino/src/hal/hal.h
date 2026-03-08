/**
 * @file hal.h
 * @brief Hardware Abstraction Layer for Arduino MCU Bridge v2.
 */
#ifndef BRIDGE_HAL_H
#define BRIDGE_HAL_H

#include <Arduino.h>
#include <stdint.h>

// [SIL-2] Undefine Arduino macros to prevent conflicts with ETL algorithms
#undef min
#undef max

#include <etl/algorithm.h>
#include <etl/span.h>
#include <etl/type_traits.h>

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

namespace detail {
// SFINAE helpers to detect push_back vs push
template <typename T, typename V>
auto push_impl(T& c, const V& v, int) -> decltype(c.push_back(v), void()) {
  c.push_back(v);
}

template <typename T, typename V>
auto push_impl(T& c, const V& v, long) -> decltype(c.push(v), void()) {
  c.push(v);
}
}  // namespace detail

/**
 * @brief Safely push data into an ETL container with capacity limits.
 * [SIL-2] Deterministic insertion using ETL algorithms.
 * @tparam TContainer ETL container type (vector, circular_buffer, etc.)
 * @param container Target container.
 * @param data Data span to insert.
 * @return Number of bytes actually inserted.
 */
template <typename TContainer>
size_t safe_push_back(TContainer& container, etl::span<const uint8_t> data) {
  if (data.empty()) return 0;
  
  const size_t cap = container.capacity();
  if (cap == 0) return 0;

  const size_t cur = container.size();
  const size_t space = cap - cur;
  const size_t len = data.size();
  const size_t to_copy = (len < space) ? len : space;

  for (size_t i = 0; i < to_copy; ++i) {
    // Use SFINAE to call either push_back() or push() based on container type
    detail::push_impl(container, data[i], 0);
  }
  return to_copy;
}

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

#endif  // BRIDGE_HAL_H
