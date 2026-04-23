/**
 * @file hal.h
 * @brief Hardware Abstraction Layer for Arduino MCU Bridge v2.
 */
#ifndef BRIDGE_HAL_H
#define BRIDGE_HAL_H

#include <Arduino.h>
#undef min
#undef max
#include <etl/expected.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <stdint.h>

namespace bridge {
inline uint32_t now_ms() { return ::millis(); }
}  // namespace bridge

namespace bridge::hal {

/**
 * @brief Error codes for HAL operations.
 */
enum class HalError {
  NONE = 0,
  IO_ERROR,
  NOT_FOUND,
  TIMEOUT,
  PERMISSION_DENIED,
  INVALID_ARGUMENT,
  NOT_IMPLEMENTED
};

struct ChunkResult {
  size_t bytes_read;
  bool has_more;
};

/**
 * @brief Force all safety-critical pins to a safe state (e.g. LOW/Input).
 */
void forceSafeState();

/**
 * @brief Get the amount of free RAM available. * @return Free bytes or
 * UINT16_MAX if detection fails.
 */
uint16_t getFreeMemory();

/**
 * @brief Validate if a pin number is valid for the current board.
 * @param pin The pin number to validate.
 * @return true if valid, false otherwise.
 */
bool isValidPin(uint8_t pin);

/**
 * @brief Initialize hardware specific features (e.g. Watchdog).
 */
void init();

/**
 * @brief Check if a functional SD card is present and initialized.
 * @return true if SD is available, false otherwise.
 */
bool hasSD();

/**
 * @brief Check if hardware SPI is available.
 */
bool hasSPI();

/**
 * @brief Write data to a file on the SD card.
 */
etl::expected<void, HalError> writeFile(etl::string_view path,
                                        etl::span<const uint8_t> data);

/**
 * @brief Read a chunk from a file on the SD card.
 */
etl::expected<ChunkResult, HalError> readFileChunk(etl::string_view path,
                                                   size_t offset,
                                                   etl::span<uint8_t> buffer);

/**
 * @brief Remove a file from the SD card.
 */
etl::expected<void, HalError> removeFile(etl::string_view path);

/**
 * @brief Get MCU capabilities bitmask.
 */
uint32_t getCapabilities();

/**
 * @brief Get the architecture specific ID.
 */
uint8_t getArchId();

/**
 * @brief Get architecture specific pin counts.
 */
void getPinCounts(uint8_t& digital, uint8_t& analog);

}  // namespace bridge::hal

// [SIL-2] Atomic Block Abstraction
#if defined(ARDUINO_ARCH_AVR)
#include <util/atomic.h>
#define BRIDGE_ATOMIC_BLOCK ATOMIC_BLOCK(ATOMIC_RESTORESTATE)
#else
struct BridgeAtomicGuard {
  BridgeAtomicGuard() {
    noInterrupts();
    asm volatile("" ::: "memory");
  }
  ~BridgeAtomicGuard() {
    asm volatile("" ::: "memory");
    interrupts();
  }
};
#define BRIDGE_ATOMIC_BLOCK if (BridgeAtomicGuard _guard{}; true)
#endif

// --- Architecture Traits ---
#include "ArchTraits.h"

#endif  // BRIDGE_HAL_H
