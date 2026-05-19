#ifndef BRIDGE_CONFIG_H
#define BRIDGE_CONFIG_H

#include <stdint.h>
#include "protocol/rpc_hw_config.h"
#include "protocol/rpc_protocol.h"

namespace bridge {
namespace config {

/**
 * [SIL-2] Hardware Abstraction Metadata
 * These values are derived from spec.toml or detected via compiler defines.
 * Strictly Zero-Redundancy: Inherits constants from generated protocol headers.
 */

#if defined(ARDUINO_ARCH_AVR)
  static constexpr bool IS_AVR = true;
  static constexpr uint8_t DIGITAL_PINS = AVR_DIGITAL_PINS; 
  static constexpr uint8_t ANALOG_PINS = AVR_ANALOG_PINS;
#elif defined(ARDUINO_ARCH_SAMD)
  static constexpr bool IS_AVR = false;
  static constexpr uint8_t DIGITAL_PINS = SAMD_DIGITAL_PINS; 
  static constexpr uint8_t ANALOG_PINS = SAMD_ANALOG_PINS;
#else
  static constexpr bool IS_AVR = false;
  static constexpr uint8_t DIGITAL_PINS = FALLBACK_MAX_PIN;
  static constexpr uint8_t ANALOG_PINS = 0; 
#endif

static constexpr bool SAFE_START_PINS_ENABLED = true;
static constexpr bool ENABLE_WATCHDOG = true;

// [SIL-2] Maximum time to wait for Linux handshake before entering safe state.
static constexpr uint32_t SYNC_TIMEOUT_MS = 30000UL;

// Static arena capacity for ArduinoJson documents — fits all payload types.
static constexpr size_t JSON_NODE_POOL_SIZE = 384U;

// --- Feature Flags (Manual overrides via build system) ---
#ifndef BRIDGE_ENABLE_DATASTORE
#define BRIDGE_ENABLE_DATASTORE 1
#endif
#ifndef BRIDGE_ENABLE_MAILBOX
#define BRIDGE_ENABLE_MAILBOX 1
#endif
#ifndef BRIDGE_ENABLE_FILESYSTEM
#define BRIDGE_ENABLE_FILESYSTEM 1
#endif
#ifndef BRIDGE_ENABLE_PROCESS
#define BRIDGE_ENABLE_PROCESS 1
#endif
#ifndef BRIDGE_ENABLE_SPI
#define BRIDGE_ENABLE_SPI 1
#endif

static constexpr bool ENABLE_DATASTORE = BRIDGE_ENABLE_DATASTORE;
static constexpr bool ENABLE_MAILBOX = BRIDGE_ENABLE_MAILBOX;
static constexpr bool ENABLE_FILESYSTEM = BRIDGE_ENABLE_FILESYSTEM;
static constexpr bool ENABLE_PROCESS = BRIDGE_ENABLE_PROCESS;
static constexpr bool ENABLE_SPI = BRIDGE_ENABLE_SPI;

}  // namespace config

namespace scheduler {
enum TimerId : uint8_t {
  TIMER_ACK_TIMEOUT = 0,
  TIMER_RX_DEDUPE = 1,
  TIMER_BAUDRATE_CHANGE = 2,
  TIMER_STARTUP_STABILIZATION = 3,
  TIMER_BOOTLOADER_DELAY = 4,
  NUMBER_OF_TIMERS = 5
};
} // namespace scheduler
}  // namespace bridge

#endif
