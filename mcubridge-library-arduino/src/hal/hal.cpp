#include "hal.h"

#include <etl/algorithm.h>
#include <etl/iterator.h>

#include "ArchTraits.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"  // IWYU pragma: keep
#include "security/security.h"

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
    } else {
      // Native or unsupported fallback
      asm volatile("");  // no-op
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
    return static_cast<uint16_t>(Traits::default_free_memory);
#endif
  } else if constexpr (Traits::id == ArchId::ARCH_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
    return static_cast<uint16_t>(ESP.getFreeHeap());
#else
    return static_cast<uint16_t>(Traits::default_free_memory);
#endif
  } else if constexpr (Traits::id == ArchId::ARCH_HOST) {
    return static_cast<uint16_t>(Traits::default_free_memory);
  }
  return static_cast<uint16_t>(Traits::default_free_memory);
}

static volatile uint32_t s_stack_canary = STACK_CANARY_VALUE;

void initStackCanary() {
#if defined(ARDUINO_ARCH_AVR)
  char* heap_ptr = (__brkval == 0 ? &__heap_start : __brkval);
  *reinterpret_cast<volatile uint32_t*>(heap_ptr) = STACK_CANARY_VALUE;
#endif
  s_stack_canary = STACK_CANARY_VALUE;
}

uint16_t getFreeStackMargin() { return getFreeMemory(); }

bool checkStackOverflow() {
#if defined(ARDUINO_ARCH_AVR)
  char* heap_ptr = (__brkval == 0 ? &__heap_start : __brkval);
  if (*reinterpret_cast<volatile uint32_t*>(heap_ptr) != STACK_CANARY_VALUE) {
    return false;
  }
#endif
  return (s_stack_canary == STACK_CANARY_VALUE) &&
         (getFreeStackMargin() >= MIN_STACK_MARGIN_BYTES);
}

static bool run_sram_march_test() {
  etl::array<uint8_t, 32> march_probe{};
  constexpr etl::array<uint8_t, 4> patterns = {0x55, 0xAA, 0x00, 0xFF};

  return etl::all_of(
      patterns.begin(), patterns.end(), [&march_probe](uint8_t pat) {
        etl::fill(march_probe.begin(), march_probe.end(), pat);
        return etl::all_of(march_probe.begin(), march_probe.end(),
                           [pat](uint8_t v) { return v == pat; });
      });
}

bool run_power_on_self_tests() {
  initStackCanary();
  const bool march_ok = run_sram_march_test();
  const bool stack_ok = checkStackOverflow();
#if BRIDGE_ENABLE_POST_TESTS
  const bool crypto_ok = rpc::security::run_cryptographic_self_tests();
  return march_ok && stack_ok && crypto_ok;
#else
  return march_ok && stack_ok;
#endif
}

void init() {
  forceSafeState();
  initStackCanary();
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

__attribute__((weak)) bool hasSPI() { return false; }

__attribute__((weak)) etl::expected<void, HalError> writeFile(
    etl::string_view, etl::span<const uint8_t>) {
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
}

__attribute__((weak)) etl::expected<ChunkResult, HalError> readFileChunk(
    etl::string_view, size_t, etl::span<uint8_t>) {
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
}

__attribute__((weak)) etl::expected<void, HalError> removeFile(
    etl::string_view) {
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
}

void fillCapabilities(rpc_pb_Capabilities& caps) {
  caps.watchdog = bridge::config::ENABLE_WATCHDOG;

#if defined(BRIDGE_ENABLE_DEBUG_FRAMES)
  caps.debug_frames = true;
#endif
#if defined(BRIDGE_ENABLE_DEBUG_IO)
  caps.debug_io = true;
#endif
#if defined(BRIDGE_ENABLE_EEPROM)
  caps.eeprom = true;
#endif
#if defined(BRIDGE_ENABLE_DAC)
  caps.dac = true;
#endif
#if defined(ARDUINO_ARCH_AVR) && defined(SERIAL_PORT_HARDWARE1)
  caps.hw_serial1 = true;
#endif
#if defined(BRIDGE_ENABLE_FPU)
  caps.fpu = true;
#endif
#if defined(BRIDGE_ENABLE_LOGIC_3V3)
  caps.logic_3v3 = true;
#endif
#if defined(BRIDGE_ENABLE_BIG_BUFFER)
  caps.big_buffer = true;
#endif
#if defined(BRIDGE_ENABLE_I2C)
  caps.i2c = (BRIDGE_ENABLE_I2C != 0);
#endif
#if defined(BRIDGE_ENABLE_SPI)
  caps.spi = (BRIDGE_ENABLE_SPI != 0);
#endif
  caps.sd = !!hasSD();
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
#endif
  }

  if constexpr (Traits::id == ArchId::ARCH_HOST) {
    // [SIL-2] On host/test environment, exit instead of hanging CI.
    exit(0);
  }

  Traits::reset();
  bridge::hal::memory_fence();
  __builtin_trap();
}

}  // namespace bridge::hal
