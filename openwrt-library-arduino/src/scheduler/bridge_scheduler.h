#ifndef BRIDGE_SCHEDULER_H
#define BRIDGE_SCHEDULER_H

#include <stdint.h>
#include "etl/array.h"
#include "etl/algorithm.h"

namespace bridge {
namespace scheduler {

// IDs for the ETL Timer System
enum TimerId : uint8_t {
  TIMER_ACK_TIMEOUT = 0,
  TIMER_RX_DEDUPE = 1,
  TIMER_BAUDRATE_CHANGE = 2,
  NUMBER_OF_TIMERS = 3
};

class TimerHandler {
public:
  virtual void on_timer(TimerId id) = 0;
};

class TimerService {
public:
    struct TimerEntry {
        TimerHandler* handler;
        TimerId id;
        uint32_t period;
        uint32_t counter;
        bool active;
        bool repeating;
    };

    TimerService() {
        clear();
    }

    void clear() {
        etl::for_each(timers_.begin(), timers_.end(), [](TimerEntry& t) {
            t.active = false;
        });
    }

    void register_timer(TimerHandler* handler, TimerId id, uint32_t period, bool repeating) {
        if (id >= NUMBER_OF_TIMERS) return;
        TimerEntry& t = timers_[id];
        t.handler = handler;
        t.id = id;
        t.period = period;
        t.counter = 0;
        t.active = true;
        t.repeating = repeating;
    }

    void unregister_timer(TimerId id) {
        if (id >= NUMBER_OF_TIMERS) return;
        timers_[id].active = false;
    }

    void tick(uint32_t delta_ms) {
        // [RAM OPT] Use ETL algorithm to iterate (might be inlined)
        // We use a lambda to process each timer.
        etl::for_each(timers_.begin(), timers_.end(), [delta_ms](TimerEntry& t) {
            if (t.active) {
                t.counter += delta_ms;
                if (t.counter >= t.period) {
                    if (t.handler) {
                        t.handler->on_timer(t.id);
                    }
                    if (t.repeating) {
                        t.counter = 0;
                    } else {
                        t.active = false;
                    }
                }
            }
        });
    }

private:
    etl::array<TimerEntry, NUMBER_OF_TIMERS> timers_;
};

} // namespace scheduler
} // namespace bridge

#endif // BRIDGE_SCHEDULER_H