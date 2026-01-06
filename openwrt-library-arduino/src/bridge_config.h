#pragma once

// Compile-time configuration for the Arduino-side library.
//
// These are *not* protocol constants (they do not affect the on-wire format).
// They control local flow-control tuning and other MCU-side implementation
// details.

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
// Defaults to 64 bytes to keep SRAM usage predictable on AVR.
#ifndef BRIDGE_CONSOLE_RX_BUFFER_SIZE
#define BRIDGE_CONSOLE_RX_BUFFER_SIZE 64
#endif

#ifndef BRIDGE_CONSOLE_TX_BUFFER_SIZE
#define BRIDGE_CONSOLE_TX_BUFFER_SIZE 64
#endif

// Pending request queue sizes (MCU-side only; not part of the protocol).
#ifndef BRIDGE_MAX_PENDING_DATASTORE
#define BRIDGE_MAX_PENDING_DATASTORE 2
#endif

#ifndef BRIDGE_MAX_PENDING_PROCESS_POLLS
#define BRIDGE_MAX_PENDING_PROCESS_POLLS 2
#endif

// File size warning threshold (bytes) - used by daemon for RAM monitoring.
// Matches Python: yunbridge.const.FILE_LARGE_WARNING_BYTES = 1048576
#ifndef BRIDGE_FILE_LARGE_WARNING_BYTES
#define BRIDGE_FILE_LARGE_WARNING_BYTES 1048576
#endif
