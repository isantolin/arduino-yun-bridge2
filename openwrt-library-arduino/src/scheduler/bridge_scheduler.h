#ifndef BRIDGE_SCHEDULER_H
#define BRIDGE_SCHEDULER_H

#include <stdint.h>
#include "etl/callback_timer.h"

namespace bridge {
namespace scheduler {

// IDs for the ETL Timer System
enum TimerId : uint8_t {
  TIMER_ACK_TIMEOUT = 0,
  TIMER_RX_DEDUPE = 1,
  TIMER_BAUDRATE_CHANGE = 2,
  TIMER_STARTUP_STABILIZATION = 3,
  NUMBER_OF_TIMERS = 4
};

// [SIL-2] Use ETL's callback_timer instead of custom implementation
using BridgeTimerService = etl::callback_timer<NUMBER_OF_TIMERS>;

} // namespace scheduler
} // namespace bridge

#endif // BRIDGE_SCHEDULER_H