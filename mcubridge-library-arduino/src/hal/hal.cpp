#include "hal.h"

#include <etl/algorithm.h>
#include <etl/binary.h>
#include <etl/bitset.h>
#include <etl/iterator.h>
#include <etl/string.h>
#include <etl/to_string.h>

#include "ArchTraits.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"

#if defined(ARDUINO_ARCH_AVR)
extern "C" {
extern char* __brkval;
extern char __heap_start;
}
#endif

namespace bridge::hal {

namespace {
using Traits = CurrentArchTraits;

constexpr uint8_t CURRENT_ARCH =
    (Traits::id == ArchId::ARCH_AVR)    ? rpc::RPC_ARCH_AVR
    : (Traits::id == ArchId::ARCH_HOST) ? rpc::RPC_ARCH_SAMD
                                        : 0;

constexpr uint8_t DIGITAL_PINS =
    (Traits::id == ArchId::ARCH_AVR)
        ? static_cast<uint8_t>(bridge::config::DIGITAL_PINS)
    : (Traits::id == ArchId::ARCH_HOST)
        ? static_cast<uint8_t>(bridge::config::SAMD_DIGITAL_PINS)
        : static_cast<uint8_t>(bridge::config::SAMD_DIGITAL_PINS);

constexpr uint8_t ANALOG_PINS =
    (Traits::id == ArchId::ARCH_AVR)
        ? static_cast<uint8_t>(bridge::config::ANALOG_PINS)
    : (Traits::id == ArchId::ARCH_HOST)
        ? static_cast<uint8_t>(bridge::config::SAMD_ANALOG_PINS)
        : 0;

}  // namespace

bool isValidPin(const uint8_t pin) { return pin < DIGITAL_PINS; }

namespace {
template <size_t I>
void _forceSinglePin() {
  if constexpr (bridge::config::SAFE_START_PINS_ENABLED) {
    ::pinMode(static_cast<uint8_t>(I), OUTPUT);
    ::digitalWrite(static_cast<uint8_t>(I), LOW);
  } else {
    ::pinMode(static_cast<uint8_t>(I), INPUT_PULLUP);
  }
}

template <size_t... Is>
void _forceSafePins(etl::index_sequence<Is...>) {
  (_forceSinglePin<Is>(), ...);
}
}  // namespace

void forceSafeState() {
  if constexpr (Traits::id == ArchId::ARCH_AVR) {
    _forceSafePins(etl::make_index_sequence<bridge::config::DIGITAL_PINS>{});
  } else {
    _forceSafePins(
        etl::make_index_sequence<bridge::config::SAMD_DIGITAL_PINS>{});
  }
}

void memory_fence() {
  // [SIL-2] Portable compiler barrier
  asm volatile("" ::: "memory");
}

void watchdog_kick() {
  if constexpr (bridge::config::ENABLE_WATCHDOG) {
    if constexpr (Traits::id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
      wdt_reset();
#endif
    } else if constexpr (Traits::id == ArchId::ARCH_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
      esp_task_wdt_reset();
#endif
    }
  }
}

uint16_t getFreeMemory() {
  if constexpr (Traits::id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
    int v;
    return static_cast<uint16_t>(
        reinterpret_cast<uintptr_t>(&v) -
        (__brkval == 0 ? reinterpret_cast<uintptr_t>(&__heap_start)
                       : reinterpret_cast<uintptr_t>(__brkval)));
#else
    return Traits::default_free_memory;
#endif
  } else if constexpr (Traits::id == ArchId::ARCH_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
    return static_cast<uint16_t>(ESP.getFreeHeap());
#else
    return Traits::default_free_memory;
#endif
  }
  return bridge::config::FALLBACK_FREE_MEMORY;
}

void init() {
  forceSafeState();
  if constexpr (bridge::config::ENABLE_WATCHDOG) {
    if constexpr (Traits::id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
      wdt_enable(WDTO_4S);
#endif
    } else if constexpr (Traits::id == ArchId::ARCH_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
      esp_task_wdt_init(4, true);
      esp_task_wdt_add(nullptr);
#endif
    }
  }
}

__attribute__((weak)) bool hasSD() { return false; }

__attribute__((weak)) etl::expected<void, HalError> writeFile(etl::string_view path,
                                        etl::span<const uint8_t> data) {
  (void)path;
  (void)data;
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
}

__attribute__((weak)) etl::expected<ChunkResult, HalError> readFileChunk(etl::string_view path,
                                                   size_t offset,
                                                   etl::span<uint8_t> buffer) {
  (void)path;
  (void)offset;
  (void)buffer;
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
}

__attribute__((weak)) etl::expected<void, HalError> removeFile(etl::string_view path) {
  (void)path;
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
}

uint32_t getCapabilities() {
  etl::bitset<32> caps;
#if BRIDGE_ENABLE_WATCHDOG
  caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_WATCHDOG));
#endif
#if BRIDGE_ENABLE_RLE
  caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_RLE));
#endif
#if defined(ARDUINO_ARCH_AVR) && defined(SERIAL_PORT_HARDWARE1)
  caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_HW_SERIAL1));
#endif
#if defined(BRIDGE_ENABLE_DAC)
  caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_DAC));
#endif
#if BRIDGE_ENABLE_I2C
  caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_I2C));
#endif
#if BRIDGE_ENABLE_SPI
  caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_SPI));
#endif
  if (hasSD()) caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_SD));
  return static_cast<uint32_t>(caps.to_ulong());
}

void getPinCounts(uint8_t& digital, uint8_t& analog) {
  digital = DIGITAL_PINS;
  analog = ANALOG_PINS;
}
uint8_t getArchId() { return CURRENT_ARCH; }

[[noreturn]] void enterBootloader() {
  forceSafeState();
  if constexpr (Traits::id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
    // [SIL-2] Caterina/Optiboot: set magic key and trigger 15 ms WDT reset.
    // The bootloader checks the token at 0x0800 on restart.
    *reinterpret_cast<volatile uint16_t*>(0x0800u) = 0x7777u;
    wdt_enable(WDTO_15MS);
#endif
  }
  // Spin until WDT fires (intentional [[noreturn]] halt).
  // Includes memory fence and WDT reset attempt to ensure clean transition.
  for (;;) {
    bridge::hal::memory_fence();
    if constexpr (bridge::config::ENABLE_WATCHDOG) {
      bridge::hal::watchdog_kick();
    }
  }
}

}  // namespace bridge::hal
