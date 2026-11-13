#pragma once

#include <stdint.h>

namespace bridge::test::fault {

enum class FaultPoint : uint8_t {
  SPI_TIMEOUT = 0,
  FILESYSTEM_TIMEOUT = 1,
  BRIDGE_POOL_ALLOC_FAIL = 2,
  BRIDGE_SERIALIZE_ZERO = 3,
  BRIDGE_NONCE_READ_FAIL = 4,
  BRIDGE_FORCE_POST_FAIL = 5,
  COUNT = 6
};

void reset();
void enable(FaultPoint point);
void disable(FaultPoint point);
bool is_enabled(FaultPoint point);
bool consume(FaultPoint point);

void set_clock_ms(uint32_t now_ms);
void advance_clock_ms(uint32_t delta_ms);
uint32_t clock_ms();

}  // namespace bridge::test::fault
