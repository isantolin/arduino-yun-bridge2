#pragma once

#include <stdint.h>
#include "protocol/rpc_hw_config.h"

/**
 * @file bridge_config.h
 * @brief Compile-time configuration for the Arduino-side library.
 */

// --- Feature Macros (MUST remain macros for #if compatibility) ---
#ifndef BRIDGE_ENABLE_DATASTORE
#define BRIDGE_ENABLE_DATASTORE 1
#endif

#ifndef BRIDGE_ENABLE_FILESYSTEM
#define BRIDGE_ENABLE_FILESYSTEM 1
#endif

#ifndef BRIDGE_ENABLE_MAILBOX
#define BRIDGE_ENABLE_MAILBOX 1
#endif

#ifndef BRIDGE_ENABLE_PROCESS
#if defined(ARDUINO_AVR_YUN) || defined(ARDUINO_AVR_UNO)
#define BRIDGE_ENABLE_PROCESS 0
#else
#define BRIDGE_ENABLE_PROCESS 1
#endif
#endif

#ifndef BRIDGE_ENABLE_SPI
#define BRIDGE_ENABLE_SPI 1
#endif

#ifndef BRIDGE_ENABLE_WATCHDOG
#if defined(BRIDGE_HOST_TEST)
#define BRIDGE_ENABLE_WATCHDOG 0
#else
#define BRIDGE_ENABLE_WATCHDOG 1
#endif
#endif

#ifndef BRIDGE_USE_USB_SERIAL
#define BRIDGE_USE_USB_SERIAL 1
#endif

namespace bridge::config {

inline constexpr bool ENABLE_DATASTORE = (BRIDGE_ENABLE_DATASTORE != 0);
inline constexpr bool ENABLE_FILESYSTEM = (BRIDGE_ENABLE_FILESYSTEM != 0);
inline constexpr bool ENABLE_MAILBOX = (BRIDGE_ENABLE_MAILBOX != 0);
inline constexpr bool ENABLE_PROCESS = (BRIDGE_ENABLE_PROCESS != 0);
inline constexpr bool ENABLE_SPI = (BRIDGE_ENABLE_SPI != 0);
inline constexpr bool ENABLE_WATCHDOG = (BRIDGE_ENABLE_WATCHDOG != 0);
inline constexpr bool USE_USB_SERIAL = (BRIDGE_USE_USB_SERIAL != 0);

// [SIL-2] If true, all digital pins will be set to OUTPUT/LOW during begin().
#ifndef BRIDGE_SAFE_START_PINS_VAL
#define BRIDGE_SAFE_START_PINS_VAL 1
#endif
inline constexpr bool SAFE_START_PINS_ENABLED = (BRIDGE_SAFE_START_PINS_VAL != 0);

}  // namespace bridge::config
