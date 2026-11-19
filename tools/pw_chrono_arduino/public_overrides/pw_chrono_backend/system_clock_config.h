// Arduino millis()-backed pw_chrono SystemClock configuration for 32-bit
// bare-metal Arduino targets (e.g. SAMD21) where newlib libstdc++'s
// std::chrono::steady_clock::now() delegates to a stub _gettimeofday() that
// returns -1 (ENOSYS) and leaves struct timeval uninitialized.
#pragma once

#define PW_CHRONO_SYSTEM_CLOCK_PERIOD_SECONDS_NUMERATOR 1
#define PW_CHRONO_SYSTEM_CLOCK_PERIOD_SECONDS_DENOMINATOR 1000

#ifdef __cplusplus

#include "pw_chrono/epoch.h"

namespace pw::chrono::backend {

inline constexpr Epoch kSystemClockEpoch = pw::chrono::Epoch::kTimeSinceBoot;
inline constexpr bool kSystemClockNmiSafe = false;
inline constexpr bool kSystemClockFreeRunning = false;

}  // namespace pw::chrono::backend

#endif  // __cplusplus
