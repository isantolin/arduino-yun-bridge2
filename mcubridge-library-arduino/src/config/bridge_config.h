#ifndef BRIDGE_CONFIG_H
#define BRIDGE_CONFIG_H

#include <stdint.h>

namespace bridge {
namespace config {

// --- Hardware Platform Detection ---
#if defined(ARDUINO_ARCH_AVR)
  static constexpr bool IS_AVR = true;
  static constexpr uint8_t DIGITAL_PINS = 20; 
  static constexpr uint8_t ANALOG_PINS = 6;
#elif defined(ARDUINO_ARCH_SAMD)
  static constexpr bool IS_AVR = false;
  static constexpr uint8_t DIGITAL_PINS = 26; 
  static constexpr uint8_t ANALOG_PINS = 7;
#else
  static constexpr bool IS_AVR = false;
  static constexpr uint8_t DIGITAL_PINS = 20;
  static constexpr uint8_t ANALOG_PINS = 6;
#endif

static constexpr uint8_t SAMD_DIGITAL_PINS = 26;
static constexpr uint8_t SAMD_ANALOG_PINS = 7;
static constexpr uint16_t FALLBACK_FREE_MEMORY = 1024;

// --- Timing & Timeouts ---
static constexpr uint32_t DEFAULT_BAUDRATE = 115200;
static constexpr uint32_t SERIAL_TIMEOUT_MS = 1000;
static constexpr uint32_t BAUDRATE_CHANGE_DELAY_MS = 50;
static constexpr uint32_t BOOTLOADER_DELAY_MS = 100;
static constexpr uint32_t STARTUP_STABILIZATION_MS = 500;
static constexpr uint32_t HANDSHAKE_RETRY_DELAY_MS = 500;
static constexpr uint16_t STARTUP_DRAIN_FINAL = 256;

// --- Reliability ---
static constexpr uint16_t DEFAULT_ACK_TIMEOUT_MS = 500;
static constexpr uint8_t DEFAULT_ACK_RETRY_LIMIT = 3;
static constexpr uint32_t DEFAULT_RESPONSE_TIMEOUT_MS = 2000;

// --- Features & Buffers ---
static constexpr uint32_t RX_DEDUPE_INTERVAL_MS = 5000;
static constexpr uint8_t RX_HISTORY_SIZE = 8;
static constexpr uint8_t TX_QUEUE_CAPACITY = 4;

static constexpr uint8_t CONSOLE_RX_BUFFER_SIZE = 64;
static constexpr uint8_t CONSOLE_TX_BUFFER_SIZE = 64;
static constexpr uint8_t MAILBOX_RX_BUFFER_SIZE = 64;
static constexpr uint8_t DATASTORE_MAX_KEYS = 8;
static constexpr uint16_t FILE_MAX_READ_CHUNKS = 64;

static constexpr uint8_t MAX_PENDING_DATASTORE = 4;
static constexpr uint8_t MAX_PENDING_PROCESS_POLLS = 4;
static constexpr uint8_t MAX_OBSERVERS = 8;

// --- Safety ---
static constexpr bool SAFE_START_PINS_ENABLED = true;
static constexpr bool ENABLE_WATCHDOG = true;
static constexpr uint8_t HKDF_KEY_LENGTH = 32;

// Feature Flags
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
