#include "hal.h"

#include <etl/algorithm.h>
#if defined(ARDUINO_ARCH_AVR)
#include <avr/pgmspace.h>
#endif

#include "ArchTraits.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"
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
    (Traits::id == ArchId::ARCH_AVR)     ? rpc::RPC_ARCH_AVR
    : (Traits::id == ArchId::ARCH_ESP32) ? rpc::RPC_ARCH_ESP32
    : (Traits::id == ArchId::ARCH_SAMD || Traits::id == ArchId::ARCH_HOST)
        ? rpc::RPC_ARCH_SAMD
        : 0;

}  // namespace

namespace {
template <size_t I>
void _forceSinglePin() {
#if defined(ARDUINO_ARCH_ESP32)
  if (!digitalPinIsValid(static_cast<uint8_t>(I))) {
    return;
  }
#endif
  if constexpr (I == PIN_SPI_SS) {
    ::pinMode(static_cast<uint8_t>(I), OUTPUT);
    ::digitalWrite(static_cast<uint8_t>(I), HIGH);
  } else if constexpr (I == PIN_SPI_SCK || I == PIN_SPI_MOSI) {
    ::pinMode(static_cast<uint8_t>(I), OUTPUT);
    ::digitalWrite(static_cast<uint8_t>(I), LOW);
  } else if constexpr (I == PIN_SPI_MISO) {
    ::pinMode(static_cast<uint8_t>(I), INPUT);
  } else if constexpr (I == PIN_WIRE_SDA || I == PIN_WIRE_SCL) {
    ::pinMode(static_cast<uint8_t>(I), INPUT_PULLUP);
  } else if constexpr (I < 2) {
    // [SIL-2] Preserve primary UART communication pins (RX/TX) for supervisor
    // link
    return;
  } else {
    if constexpr (bridge::config::SAFE_START_PINS_ENABLED) {
#if defined(ARDUINO_ARCH_ESP32)
      if (!digitalPinCanOutput(static_cast<uint8_t>(I))) {
        return;
      }
#endif
      ::pinMode(static_cast<uint8_t>(I), OUTPUT);
      ::digitalWrite(static_cast<uint8_t>(I), LOW);
    } else {
      ::pinMode(static_cast<uint8_t>(I), INPUT_PULLUP);
    }
  }
}

template <size_t... Is>
void _forceSafePins(etl::index_sequence<Is...>) {
  (_forceSinglePin<Is>(), ...);
}
}  // namespace

void forceSafeState() {
  _forceSafePins(etl::make_index_sequence<NUM_DIGITAL_PINS>{});
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

uint32_t getFreeMemory() {
  if constexpr (Traits::id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
    int v;
    return static_cast<uint32_t>(
        reinterpret_cast<uintptr_t>(&v) -
        (__brkval == 0 ? reinterpret_cast<uintptr_t>(&__heap_start)
                       : reinterpret_cast<uintptr_t>(__brkval)));
#endif
  } else if constexpr (Traits::id == ArchId::ARCH_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
    return static_cast<uint32_t>(ESP.getFreeHeap());
#endif
  }
  return Traits::default_free_memory;
}

static volatile uint32_t s_stack_canary = STACK_CANARY_VALUE;

void initStackCanary() {
#if defined(ARDUINO_ARCH_AVR)
  char* heap_ptr = (__brkval == 0 ? &__heap_start : __brkval);
  *reinterpret_cast<volatile uint32_t*>(heap_ptr) = STACK_CANARY_VALUE;
#endif
  s_stack_canary = STACK_CANARY_VALUE;
}

bool checkStackOverflow() {
#if defined(ARDUINO_ARCH_AVR)
  char* heap_ptr = (__brkval == 0 ? &__heap_start : __brkval);
  if (*reinterpret_cast<volatile uint32_t*>(heap_ptr) != STACK_CANARY_VALUE) {
    return false;
  }
#endif
  return (s_stack_canary == STACK_CANARY_VALUE) &&
         (getFreeMemory() >= MIN_STACK_MARGIN_BYTES);
}

static bool run_sram_march_test() {
  etl::array<uint8_t, 32> march_probe{};
#if defined(ARDUINO_ARCH_AVR)
  static const uint8_t patterns[] PROGMEM = {0x55, 0xAA, 0x00, 0xFF};
#else
  static constexpr uint8_t patterns[] = {0x55, 0xAA, 0x00, 0xFF};
#endif
  for (size_t i = 0; i < sizeof(patterns) / sizeof(patterns[0]); ++i) {
#if defined(ARDUINO_ARCH_AVR)
    const uint8_t pat = pgm_read_byte(&patterns[i]);
#else
    const uint8_t pat = patterns[i];
#endif
    etl::fill(march_probe.begin(), march_probe.end(), pat);
    if (!etl::all_of(march_probe.begin(), march_probe.end(),
                     [pat](uint8_t v) { return v == pat; })) return false;
  }
  return true;
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
      // ESP-IDF 5 (Espressif Arduino core 3.x) configures the task watchdog
      // with a struct. The core usually starts the TWDT already, so reconfigure
      // it and fall back to init only when it is not running yet.
      const esp_task_wdt_config_t twdt_config = {
          .timeout_ms = 4000U,
          .idle_core_mask = 0U,
          .trigger_panic = true,
      };
      if (esp_task_wdt_reconfigure(&twdt_config) != ESP_OK) {
        (void)esp_task_wdt_init(&twdt_config);
      }
      (void)esp_task_wdt_add(nullptr);
#endif
    }
  }
}

__attribute__((weak)) bool hasSD() { return false; }

__attribute__((weak)) pw::Status writeFile(etl::string_view,
                                           pw::span<const uint8_t>) {
  return pw::Status::Unimplemented();
}

__attribute__((weak)) pw::Result<ChunkResult> readFileChunk(etl::string_view,
                                                            size_t,
                                                            pw::span<uint8_t>) {
  return pw::Status::Unimplemented();
}

__attribute__((weak)) pw::Status removeFile(etl::string_view) {
  return pw::Status::Unimplemented();
}

void applySafetyPin(uint8_t pin, SafetyPinState state) {
#if defined(ARDUINO_ARCH_ESP32)
  if (!digitalPinIsValid(pin)) return;
#endif
  if (state == SafetyPinState::SAFE_PIN_LOW ||
      state == SafetyPinState::SAFE_PIN_HIGH) {
#if defined(ARDUINO_ARCH_ESP32)
    if (!digitalPinCanOutput(pin)) return;
#endif
    ::pinMode(pin, OUTPUT);
    ::digitalWrite(pin, state == SafetyPinState::SAFE_PIN_HIGH ? HIGH : LOW);
  } else if (state == SafetyPinState::SAFE_PIN_INPUT_PULLUP) {
    ::pinMode(pin, INPUT_PULLUP);
  } else {
    ::pinMode(pin, INPUT);
  }
}

void fillCapabilities(rpc_pb_Capabilities& caps) {
  caps.ver = rpc::PROTOCOL_VERSION;
  caps.arch = CURRENT_ARCH;
  caps.dig = NUM_DIGITAL_PINS;
  caps.ana = NUM_ANALOG_INPUTS;
  caps.watchdog = bridge::config::ENABLE_WATCHDOG;

#if defined(ARDUINO_AVR_MEGA2560) || defined(ARDUINO_AVR_MEGA)
#define BRIDGE_BOARD_NAME "MEGA2560"
#elif defined(ARDUINO_AVR_YUN)
#define BRIDGE_BOARD_NAME "YUN"
#elif defined(ARDUINO_AVR_LEONARDO)
#define BRIDGE_BOARD_NAME "LEONARDO"
#elif defined(ARDUINO_AVR_UNO)
#define BRIDGE_BOARD_NAME "UNO"
#elif defined(ARDUINO_ARCH_SAMD)
#define BRIDGE_BOARD_NAME "SAMD21"
#elif defined(ARDUINO_ARCH_ESP32)
#define BRIDGE_BOARD_NAME "ESP32"
#else
#define BRIDGE_BOARD_NAME "GENERIC"
#endif
#if defined(ARDUINO_ARCH_AVR)
static const char board_name[] PROGMEM = BRIDGE_BOARD_NAME;
#else
static constexpr char board_name[] = BRIDGE_BOARD_NAME;
#endif
  const size_t len = etl::min(sizeof(board_name) - 1, sizeof(caps.board_name) - 1);
  for (size_t i = 0; i < len; ++i) {
#if defined(ARDUINO_ARCH_AVR)
    caps.board_name[i] = static_cast<char>(pgm_read_byte(&board_name[i]));
#else
    caps.board_name[i] = board_name[i];
#endif
  }
  caps.board_name[len] = '\0';
#undef BRIDGE_BOARD_NAME

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
#if defined(ARDUINO_ARCH_AVR) && defined(HAVE_HWSERIAL1)
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
  caps.clock_sync = true;
}

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
