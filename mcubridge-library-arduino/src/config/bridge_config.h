#ifndef BRIDGE_CONFIG_H
#define BRIDGE_CONFIG_H

#include <Arduino.h>
#include <stdint.h>

#include "protocol/rpc_hw_config.h"
#include "protocol/rpc_protocol.h"

#if defined(ARDUINO_ARCH_ESP32)
#ifndef PIN_SPI_SS
#define PIN_SPI_SS SS
#endif
#ifndef PIN_SPI_MOSI
#define PIN_SPI_MOSI MOSI
#endif
#ifndef PIN_SPI_MISO
#define PIN_SPI_MISO MISO
#endif
#ifndef PIN_SPI_SCK
#define PIN_SPI_SCK SCK
#endif
#ifndef PIN_WIRE_SDA
#define PIN_WIRE_SDA SDA
#endif
#ifndef PIN_WIRE_SCL
#define PIN_WIRE_SCL SCL
#endif
#endif

namespace bridge {
namespace config {

// Pin counts come from the selected Arduino core.
#if !defined(NUM_DIGITAL_PINS) || !defined(NUM_ANALOG_INPUTS)
#error "The Arduino core must define NUM_DIGITAL_PINS and NUM_ANALOG_INPUTS"
#endif
#if !defined(PIN_SPI_SS) || !defined(PIN_SPI_MOSI) ||  \
    !defined(PIN_SPI_MISO) || !defined(PIN_SPI_SCK) || \
    !defined(PIN_WIRE_SDA) || !defined(PIN_WIRE_SCL)
#error "The Arduino core must define native SPI and I2C pin identifiers"
#endif
static_assert(NUM_DIGITAL_PINS <= UINT8_MAX);
static_assert(NUM_ANALOG_INPUTS <= UINT8_MAX);
static_assert(PIN_SPI_SS < NUM_DIGITAL_PINS);
static_assert(PIN_SPI_MOSI < NUM_DIGITAL_PINS);
static_assert(PIN_SPI_MISO < NUM_DIGITAL_PINS);
static_assert(PIN_SPI_SCK < NUM_DIGITAL_PINS);
static_assert(PIN_WIRE_SDA < NUM_DIGITAL_PINS);
static_assert(PIN_WIRE_SCL < NUM_DIGITAL_PINS);
inline constexpr bool SAFE_START_PINS_ENABLED = true;
inline constexpr bool ENABLE_WATCHDOG = true;

// [SIL-2] Maximum time to wait for Linux handshake before entering safe state.
inline constexpr uint32_t SYNC_TIMEOUT_MS = rpc::SYNC_TIMEOUT_MS;

#ifndef MAILBOX_QUEUE_CAPACITY
#define MAILBOX_QUEUE_CAPACITY 4  // Capacidad de elementos en la cola
#endif

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

inline constexpr bool ENABLE_DATASTORE = BRIDGE_ENABLE_DATASTORE;
inline constexpr bool ENABLE_MAILBOX = BRIDGE_ENABLE_MAILBOX;
inline constexpr bool ENABLE_FILESYSTEM = BRIDGE_ENABLE_FILESYSTEM;
inline constexpr bool ENABLE_PROCESS = BRIDGE_ENABLE_PROCESS;
inline constexpr bool ENABLE_SPI = BRIDGE_ENABLE_SPI;

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
