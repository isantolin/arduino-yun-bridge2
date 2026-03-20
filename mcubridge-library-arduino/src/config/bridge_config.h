#pragma once

#include <stdint.h>

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

#ifndef BRIDGE_ENABLE_WATCHDOG
#define BRIDGE_ENABLE_WATCHDOG 1
#endif

#ifndef BRIDGE_USE_USB_SERIAL
#define BRIDGE_USE_USB_SERIAL 1
#endif

// --- Internal Constants ---
#ifndef BRIDGE_MAX_OBSERVERS_VAL
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_MAX_OBSERVERS_VAL 2
#else
#define BRIDGE_MAX_OBSERVERS_VAL 4
#endif
#endif

#ifndef BRIDGE_RX_HISTORY_SIZE_VAL
#define BRIDGE_RX_HISTORY_SIZE_VAL 1
#endif

#ifndef BRIDGE_RX_BUFFER_SIZE_VAL
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_RX_BUFFER_SIZE_VAL 32
#else
#define BRIDGE_RX_BUFFER_SIZE_VAL 128
#endif
#endif

#ifndef BRIDGE_MAX_PENDING_TX_FRAMES_VAL
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_MAX_PENDING_TX_FRAMES_VAL 1
#else
#define BRIDGE_MAX_PENDING_TX_FRAMES_VAL 2
#endif
#endif

#ifndef BRIDGE_CONSOLE_RX_BUFFER_SIZE_VAL
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_CONSOLE_RX_BUFFER_SIZE_VAL 4
#else
#define BRIDGE_CONSOLE_RX_BUFFER_SIZE_VAL 64
#endif
#endif

#ifndef BRIDGE_CONSOLE_TX_BUFFER_SIZE_VAL
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_CONSOLE_TX_BUFFER_SIZE_VAL 4
#else
// MAX_PAYLOAD_SIZE is 64. Protobuf tags take ~2 bytes. Safe value is 60.
#define BRIDGE_CONSOLE_TX_BUFFER_SIZE_VAL 60
#endif
#endif

#ifndef BRIDGE_MAILBOX_RX_BUFFER_SIZE_VAL
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_MAILBOX_RX_BUFFER_SIZE_VAL 4
#else
#define BRIDGE_MAILBOX_RX_BUFFER_SIZE_VAL 128
#endif
#endif

namespace bridge {
namespace config {

static constexpr bool ENABLE_DATASTORE = (BRIDGE_ENABLE_DATASTORE != 0);
static constexpr bool ENABLE_FILESYSTEM = (BRIDGE_ENABLE_FILESYSTEM != 0);
static constexpr bool ENABLE_MAILBOX = (BRIDGE_ENABLE_MAILBOX != 0);
static constexpr bool ENABLE_PROCESS = (BRIDGE_ENABLE_PROCESS != 0);
static constexpr bool ENABLE_WATCHDOG = (BRIDGE_ENABLE_WATCHDOG != 0);
static constexpr bool USE_USB_SERIAL = (BRIDGE_USE_USB_SERIAL != 0);

static constexpr uint16_t MAX_OBSERVERS = BRIDGE_MAX_OBSERVERS_VAL;
static constexpr uint16_t RX_HISTORY_SIZE = BRIDGE_RX_HISTORY_SIZE_VAL;
static constexpr uint16_t RX_BUFFER_SIZE = BRIDGE_RX_BUFFER_SIZE_VAL;
static constexpr uint16_t MAX_PENDING_TX_FRAMES = BRIDGE_MAX_PENDING_TX_FRAMES_VAL;

static constexpr uint16_t CONSOLE_RX_BUFFER_SIZE = BRIDGE_CONSOLE_RX_BUFFER_SIZE_VAL;
static constexpr uint16_t CONSOLE_TX_BUFFER_SIZE = BRIDGE_CONSOLE_TX_BUFFER_SIZE_VAL;
static constexpr uint16_t MAILBOX_RX_BUFFER_SIZE = BRIDGE_MAILBOX_RX_BUFFER_SIZE_VAL;

static constexpr uint16_t MAX_PENDING_DATASTORE = 1U;
static constexpr uint16_t MAX_PENDING_PROCESS_POLLS = 1U;
static constexpr uint32_t FILE_LARGE_WARNING_BYTES = 1048576UL;
static constexpr uint16_t STARTUP_DRAIN_PER_TICK = 64U;
static constexpr uint16_t STARTUP_DRAIN_FINAL = 256U;
static constexpr uint32_t STARTUP_STABILIZATION_MS = 100UL;
static constexpr uint32_t BAUDRATE_SETTLE_MS = 50UL;
static constexpr uint16_t MAX_CONSECUTIVE_CRC_ERRORS = 5U;
static constexpr uint32_t RX_DEDUPE_INTERVAL_MS = 1000UL;
static constexpr uint16_t HKDF_KEY_LENGTH = 32U;
static constexpr uint16_t KEY_AND_DIGEST_BUFFER_SIZE = 64U;

// --- Hardware pin defaults (when NUM_DIGITAL_PINS is unavailable) ---
static constexpr uint8_t AVR_DIGITAL_PINS = 14U;
static constexpr uint8_t AVR_ANALOG_PINS = 6U;
static constexpr uint8_t SAMD_DIGITAL_PINS = 20U;
static constexpr uint8_t SAMD_ANALOG_PINS = 8U;
static constexpr uint8_t FALLBACK_MAX_PIN = 32U;
static constexpr uint16_t FALLBACK_FREE_MEMORY = 1024U;
static constexpr uint32_t CAPABILITIES_FEAT_EXTENDED = 0x01U;

}  // namespace config
}  // namespace bridge
