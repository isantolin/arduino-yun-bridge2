#include "BridgeFaultInjection.h"

#include <atomic>

namespace bridge::test::fault {
namespace {

constexpr uint32_t bit_for(FaultPoint point) {
  return 1UL << static_cast<uint8_t>(point);
}

std::atomic<uint32_t> g_fault_mask{0U};
std::atomic<uint32_t> g_clock_ms{0U};

}  // namespace

void reset() {
  g_fault_mask.store(0U, std::memory_order_relaxed);
  g_clock_ms.store(0U, std::memory_order_relaxed);
}

void enable(FaultPoint point) {
  g_fault_mask.fetch_or(bit_for(point), std::memory_order_relaxed);
}

void disable(FaultPoint point) {
  g_fault_mask.fetch_and(~bit_for(point), std::memory_order_relaxed);
}

bool is_enabled(FaultPoint point) {
  return (g_fault_mask.load(std::memory_order_relaxed) & bit_for(point)) != 0U;
}

bool consume(FaultPoint point) {
  const uint32_t bit = bit_for(point);
  uint32_t current = g_fault_mask.load(std::memory_order_relaxed);
  while ((current & bit) != 0U) {
    if (g_fault_mask.compare_exchange_weak(current, current & ~bit,
                                           std::memory_order_relaxed,
                                           std::memory_order_relaxed)) {
      return true;
    }
  }
  return false;
}

void set_clock_ms(uint32_t now_ms) {
  g_clock_ms.store(now_ms, std::memory_order_relaxed);
}

void advance_clock_ms(uint32_t delta_ms) {
  g_clock_ms.fetch_add(delta_ms, std::memory_order_relaxed);
}

uint32_t clock_ms() { return g_clock_ms.load(std::memory_order_relaxed); }

}  // namespace bridge::test::fault
