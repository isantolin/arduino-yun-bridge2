#ifndef HARDWARE_ABSTRACTION_H
#define HARDWARE_ABSTRACTION_H

#include <Arduino.h>

namespace bridge {
namespace hardware {

void initWatchdog();
void resetWatchdog();
uint16_t getFreeMemory();

} // namespace hardware
} // namespace bridge

#endif // HARDWARE_ABSTRACTION_H
