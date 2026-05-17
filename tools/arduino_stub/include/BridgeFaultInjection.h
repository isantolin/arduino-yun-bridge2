#pragma once

#include <stdint.h>

namespace bridge::test::fault {

enum class FaultPoint : uint8_t {
  SPI_TIMEOUT = 0,
  FILESYSTEM_TIMEOUT = 1,
  KAT_SHA256_MISMATCH = 2,
  KAT_HMAC_MISMATCH = 3,
  KAT_AEAD_FAIL = 4,
  BRIDGE_POOL_ALLOC_FAIL = 5,
  BRIDGE_SERIALIZE_ZERO = 6,
  BRIDGE_NONCE_READ_FAIL = 7,
  BRIDGE_FORCE_POST_FAIL = 8,
  COUNT = 9
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
