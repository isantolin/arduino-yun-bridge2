// Monotonic 64-bit millisecond SystemClock backend backed by Arduino millis()
// for 32-bit bare-metal Arduino cores (SAMD21 Cortex-M0+).
//
// Arduino millis() returns a 32-bit unsigned millisecond counter that wraps
// every ~49.7 days. Tracking wrap-arounds under an interrupt-masked critical
// section preserves the pw_chrono SystemClock monotonicity and IRQ-safety
// contracts across rollovers.
#include "pw_chrono/system_clock.h"

#include <cstdint>

extern "C" unsigned long millis(void);

namespace pw::chrono::backend {
namespace {

#if defined(__arm__) || defined(__thumb__)
class InterruptGuard {
 public:
  InterruptGuard() {
    __asm__ volatile("mrs %0, primask\n\tcpsid i"
                     : "=r"(primask_)
                     :
                     : "memory");
  }
  ~InterruptGuard() {
    __asm__ volatile("msr primask, %0" : : "r"(primask_) : "memory");
  }

  InterruptGuard(const InterruptGuard&) = delete;
  InterruptGuard& operator=(const InterruptGuard&) = delete;

 private:
  uint32_t primask_ = 0;
};
#endif

uint32_t g_last_millis = 0;
uint32_t g_overflow_count = 0;

}  // namespace

int64_t GetSystemClockTickCount() {
#if defined(__arm__) || defined(__thumb__)
  InterruptGuard guard;
#endif
  const uint32_t now_ms = static_cast<uint32_t>(millis());
  if (now_ms < g_last_millis) {
    ++g_overflow_count;
  }
  g_last_millis = now_ms;
  return static_cast<int64_t>((static_cast<uint64_t>(g_overflow_count) << 32U) |
                              static_cast<uint64_t>(now_ms));
}

}  // namespace pw::chrono::backend
