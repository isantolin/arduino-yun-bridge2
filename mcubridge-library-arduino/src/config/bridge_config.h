#pragma once

#include <stdint.h>

/**
 * @file bridge_config.h
 * @brief Compile-time configuration for the Arduino-side library.
 *
 * [SIL-2 COMPLIANCE]
 * Configuration is defined using constexpr to ensure type safety and
 * compile-time evaluation. Macros are avoided to prevent namespace pollution
 * and allow the compiler to perform better static analysis.
 */

namespace bridge {
namespace config {

// --- Subsystem Enablement (RAM Optimization) ---
// [SIL-2] Centralized here to ensure consistent class layout (ODR compliance)

#ifdef BRIDGE_ENABLE_DATASTORE
static constexpr bool ENABLE_DATASTORE = (BRIDGE_ENABLE_DATASTORE != 0);
#else
static constexpr bool ENABLE_DATASTORE = true;
#endif

#ifdef BRIDGE_ENABLE_FILESYSTEM
static constexpr bool ENABLE_FILESYSTEM = (BRIDGE_ENABLE_FILESYSTEM != 0);
#else
static constexpr bool ENABLE_FILESYSTEM = true;
#endif

#ifdef BRIDGE_ENABLE_MAILBOX
static constexpr bool ENABLE_MAILBOX = (BRIDGE_ENABLE_MAILBOX != 0);
#else
static constexpr bool ENABLE_MAILBOX = true;
#endif

#ifdef BRIDGE_ENABLE_PROCESS
static constexpr bool ENABLE_PROCESS = (BRIDGE_ENABLE_PROCESS != 0);
#else
static constexpr bool ENABLE_PROCESS = true;
#endif

// [SIL-2] Resource Allocation Tuning
// On memory constrained AVR (Mega/Yun), limit the pending queue to 1 frame.
#if defined(ARDUINO_ARCH_AVR)
static constexpr uint16_t MAX_PENDING_TX_FRAMES = 1U;
#else
#include "../protocol/rpc_protocol.h"
static constexpr uint16_t MAX_PENDING_TX_FRAMES =
    static_cast<uint16_t>(rpc::RPC_MAX_PENDING_TX_FRAMES + 1);
#endif

// Assumed RX buffer size for the underlying serial implementation.
static constexpr uint16_t HW_RX_BUFFER_SIZE = 64U;

// High/low watermarks to emit XOFF/XON based on bytes available.
static constexpr uint16_t RX_HIGH_WATER_MARK = (HW_RX_BUFFER_SIZE * 3) / 4;
static constexpr uint16_t RX_LOW_WATER_MARK = (HW_RX_BUFFER_SIZE * 1) / 4;

// Console ring buffers (MCU-side only; not part of the protocol).
#if defined(ARDUINO_ARCH_AVR)
static constexpr uint16_t CONSOLE_RX_BUFFER_SIZE = 8U;
static constexpr uint16_t CONSOLE_TX_BUFFER_SIZE = 8U;
static constexpr uint16_t MAILBOX_RX_BUFFER_SIZE = 8U;
#else
static constexpr uint16_t CONSOLE_RX_BUFFER_SIZE = 64U;
static constexpr uint16_t CONSOLE_TX_BUFFER_SIZE = 64U;
static constexpr uint16_t MAILBOX_RX_BUFFER_SIZE = 128U;
#endif

// Pending request queue sizes (MCU-side only; not part of the protocol).
static constexpr uint16_t MAX_PENDING_DATASTORE = 1U;
static constexpr uint16_t MAX_PENDING_PROCESS_POLLS = 1U;

// File size warning threshold (bytes)
static constexpr uint32_t FILE_LARGE_WARNING_BYTES = 1048576UL;

// [SIL-2] Startup drain limits
static constexpr uint16_t STARTUP_DRAIN_PER_TICK = 64U;
static constexpr uint16_t STARTUP_DRAIN_FINAL = 256U;

// [SIL-2] Timing constants
static constexpr uint32_t STARTUP_STABILIZATION_MS = 100UL;
static constexpr uint32_t BAUDRATE_SETTLE_MS = 50UL;
static constexpr uint16_t MAX_CONSECUTIVE_CRC_ERRORS = 5U;
static constexpr uint32_t RX_DEDUPE_INTERVAL_MS = 1000UL;
static constexpr uint16_t RX_HISTORY_SIZE = 1U;

// [SIL-2] HMAC key derivation buffer sizes
static constexpr uint16_t HKDF_KEY_LENGTH = 32U;
static constexpr uint16_t KEY_AND_DIGEST_BUFFER_SIZE = 64U;

// [SIL-2] Serial Port Configuration
static constexpr bool USE_USB_SERIAL = true;

}  // namespace config
}  // namespace bridge
