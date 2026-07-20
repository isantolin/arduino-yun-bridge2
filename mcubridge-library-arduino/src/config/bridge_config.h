#ifndef BRIDGE_CONFIG_H
#define BRIDGE_CONFIG_H

#include <stdint.h>

#include "protocol/rpc_hw_config.h"
#include "protocol/rpc_protocol.h"

namespace bridge {
namespace config {

/**
 * [SIL-2] Hardware Abstraction Metadata
 * These values are derived from mcubridge.proto or detected via compiler
 * defines. Strictly Zero-Redundancy: Inherits constants from generated protocol
 * headers.
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
static constexpr uint8_t ANALOG_PINS = AVR_ANALOG_PINS;
#endif

static constexpr bool SAFE_START_PINS_ENABLED = true;
static constexpr bool ENABLE_WATCHDOG = true;

// [SIL-2] Maximum time to wait for Linux handshake before entering safe state.
static constexpr uint32_t SYNC_TIMEOUT_MS = rpc::SYNC_TIMEOUT_MS;

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

// [SIL-2/AVR] Cryptographic Power-On Self-Tests (KAT for SHA256, HMAC, AEAD).
// Enabled by default. Set to 0 for flash-constrained targets (e.g. ATmega328P).
#ifndef BRIDGE_ENABLE_POST_TESTS
#define BRIDGE_ENABLE_POST_TESTS 1
#endif

}  // namespace config

namespace scheduler {
enum TimerId : uint8_t {
  TIMER_ACK_TIMEOUT = 0,
  TIMER_RX_DEDUPE = 1,
  TIMER_BAUDRATE_CHANGE = 2,
  TIMER_BOOTLOADER_DELAY = 3,
  TIMER_HANDSHAKE_TIMEOUT = 4,  // [SIL-2/H-2] Handshake response watchdog
  NUMBER_OF_TIMERS = 5
};
}  // namespace scheduler
}  // namespace bridge

#endif
