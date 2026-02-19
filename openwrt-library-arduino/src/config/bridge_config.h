#pragma once

// Compile-time configuration for the Arduino-side library.
//
// These are *not* protocol constants (they do not affect the on-wire format).
// They control local flow-control tuning and other MCU-side implementation
// details.

// --- Subsystem Enablement (RAM Optimization) ---
// [SIL-2] Centralized here to ensure consistent class layout (ODR compliance)

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
#define BRIDGE_ENABLE_PROCESS 1
#endif

// Assumed RX buffer size for the underlying serial implementation.
// AVR HardwareSerial uses 64 bytes by default in many cores.
#ifndef BRIDGE_HW_RX_BUFFER_SIZE
#define BRIDGE_HW_RX_BUFFER_SIZE 64
#endif

// High/low watermarks to emit XOFF/XON based on bytes available.
// Defaults: 75% / 25% of the assumed HW RX buffer.
#ifndef BRIDGE_RX_HIGH_WATER_MARK
#define BRIDGE_RX_HIGH_WATER_MARK ((BRIDGE_HW_RX_BUFFER_SIZE * 3) / 4)
#endif

#ifndef BRIDGE_RX_LOW_WATER_MARK
#define BRIDGE_RX_LOW_WATER_MARK ((BRIDGE_HW_RX_BUFFER_SIZE * 1) / 4)
#endif

// Console ring buffers (MCU-side only; not part of the protocol).
// Defaults to 48 bytes to keep SRAM usage predictable on AVR.
#if defined(ARDUINO_ARCH_AVR)
  // [SIL-2] Reduce console buffers for AVR to save ~32 bytes
  // Increased from 16 to 32 to allow small batches of messages without frequent XOFF.
  #ifndef BRIDGE_CONSOLE_RX_BUFFER_SIZE
  #define BRIDGE_CONSOLE_RX_BUFFER_SIZE 32U
  #endif

  #ifndef BRIDGE_CONSOLE_TX_BUFFER_SIZE
  #define BRIDGE_CONSOLE_TX_BUFFER_SIZE 32U
  #endif
#else
  #ifndef BRIDGE_CONSOLE_RX_BUFFER_SIZE
  #define BRIDGE_CONSOLE_RX_BUFFER_SIZE 64U
  #endif

  #ifndef BRIDGE_CONSOLE_TX_BUFFER_SIZE
  #define BRIDGE_CONSOLE_TX_BUFFER_SIZE 64U
  #endif
#endif

// Pending request queue sizes (MCU-side only; not part of the protocol).
#ifndef BRIDGE_MAX_PENDING_DATASTORE
#define BRIDGE_MAX_PENDING_DATASTORE 1U
#endif

#ifndef BRIDGE_MAX_PENDING_PROCESS_POLLS
#define BRIDGE_MAX_PENDING_PROCESS_POLLS 1U
#endif

// File size warning threshold (bytes) - used by daemon for RAM monitoring.
// Matches Python: mcubridge.const.FILE_LARGE_WARNING_BYTES = 1048576
#ifndef BRIDGE_FILE_LARGE_WARNING_BYTES
#define BRIDGE_FILE_LARGE_WARNING_BYTES 1048576UL
#endif

// [SIL-2] Magic Numbers extracted to constants for clarity and safety tuning
#ifndef BRIDGE_STARTUP_STABILIZATION_MS
#define BRIDGE_STARTUP_STABILIZATION_MS 100UL
#endif

#ifndef BRIDGE_BAUDRATE_SETTLE_MS
#define BRIDGE_BAUDRATE_SETTLE_MS 50UL
#endif

#ifndef BRIDGE_MAX_CONSECUTIVE_CRC_ERRORS
#define BRIDGE_MAX_CONSECUTIVE_CRC_ERRORS 5U
#endif

// [SIL-2] RX Deduplication reset interval (ms)
// After this period, the same CRC will be accepted again (retry recovery)
#ifndef BRIDGE_RX_DEDUPE_INTERVAL_MS
#define BRIDGE_RX_DEDUPE_INTERVAL_MS 1000UL
#endif

// [SIL-2] HMAC key derivation buffer sizes (SHA256 specific)
// Buffer holds handshake_key (32 bytes) + digest (32 bytes)
#ifndef BRIDGE_HKDF_KEY_LENGTH
#define BRIDGE_HKDF_KEY_LENGTH 32
#endif

#ifndef BRIDGE_KEY_AND_DIGEST_BUFFER_SIZE
#define BRIDGE_KEY_AND_DIGEST_BUFFER_SIZE 64
#endif

// [SIL-2] Serial Port Configuration
// Force Bridge to use the USB CDC port (Serial) instead of Hardware UART (Serial1)
// on compatible boards (Yun, Leonardo, etc.).
// Essential for direct PC-to-MCU connection debugging.
#ifndef BRIDGE_USE_USB_SERIAL
#define BRIDGE_USE_USB_SERIAL 1
#endif
