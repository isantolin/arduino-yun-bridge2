#include "hal.h"

#include <etl/algorithm.h>
#include <etl/array.h>
#include <pw_bytes/endian.h>
#if defined(ARDUINO_ARCH_AVR)
#include <EEPROM.h>
#include <avr/pgmspace.h>
#endif
#if defined(ARDUINO_ARCH_ESP32)
#include <esp_random.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#endif
#if defined(ARDUINO_ARCH_SAMD)
#include <FlashStorage.h>
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
#if defined(ARDUINO_ARCH_SAMD)
extern "C" {
extern uint32_t __bss_end__;
extern uint32_t __StackTop;
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
  constexpr uint8_t pin = static_cast<uint8_t>(I);
#if defined(ARDUINO_ARCH_ESP32)
  if (!digitalPinIsValid(pin)) return;
#endif
  if constexpr (I == PIN_SPI_SS) {
    pw::digital_io::ArduinoDigitalOut output(
        pin, pw::digital_io::State::kActive);
    (void)output.Enable();
  } else if constexpr (I == PIN_SPI_SCK || I == PIN_SPI_MOSI) {
    pw::digital_io::ArduinoDigitalOut output(
        pin, pw::digital_io::State::kInactive);
    (void)output.Enable();
  } else if constexpr (I == PIN_SPI_MISO) {
    pw::digital_io::ArduinoDigitalIn input(pin);
    (void)input.Enable();
  } else if constexpr (I == PIN_WIRE_SDA || I == PIN_WIRE_SCL) {
    pw::digital_io::ArduinoDigitalIn input(pin, true);
    (void)input.Enable();
  } else if constexpr (I < 2) {
    // Preserve the supervisor UART's RX/TX pins.
    return;
  } else if constexpr (bridge::config::SAFE_START_PINS_ENABLED) {
#if defined(ARDUINO_ARCH_ESP32)
    if (!digitalPinCanOutput(pin)) return;
#endif
    pw::digital_io::ArduinoDigitalOut output(
        pin, pw::digital_io::State::kInactive);
    (void)output.Enable();
  } else {
    pw::digital_io::ArduinoDigitalIn input(pin, true);
    (void)input.Enable();
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
  } else if constexpr (Traits::id == ArchId::ARCH_SAMD) {
#if defined(ARDUINO_ARCH_SAMD)
    volatile uint8_t sp_probe = 0;
    const uintptr_t sp_addr = reinterpret_cast<uintptr_t>(&sp_probe);
    const uintptr_t bss_end = reinterpret_cast<uintptr_t>(&__bss_end__);
    return (sp_addr > bss_end) ? static_cast<uint32_t>(sp_addr - bss_end) : 0U;
#endif
  } else if constexpr (Traits::id == ArchId::ARCH_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
    return static_cast<uint32_t>(ESP.getFreeHeap());
#endif
  }
  return Traits::default_free_memory;
}

static volatile uint32_t s_stack_canary = STACK_CANARY_VALUE;
#if defined(ARDUINO_ARCH_SAMD)
namespace {
constexpr uint8_t kSamdStackPaintByte = 0xA5U;
constexpr uintptr_t kSamdStackPaintSafetyBytes = 64U;
bool s_samd_stack_painted = false;

__attribute__((noinline)) void paintSamdFreeStack() {
  volatile uint8_t sp_probe = 0;
  const uintptr_t sp_addr = reinterpret_cast<uintptr_t>(&sp_probe);
  const uintptr_t paint_start = reinterpret_cast<uintptr_t>(&__bss_end__);
  if (sp_addr <= paint_start + kSamdStackPaintSafetyBytes) {
    return;
  }
  const uintptr_t paint_end = sp_addr - kSamdStackPaintSafetyBytes;
  uint8_t* ptr = reinterpret_cast<uint8_t*>(paint_start);
  uint8_t* const end = reinterpret_cast<uint8_t*>(paint_end);
  etl::fill(ptr, end, kSamdStackPaintByte);
  s_samd_stack_painted = true;
}
}  // namespace
#endif

void initStackCanary() {
#if defined(ARDUINO_ARCH_AVR)
  char* heap_ptr = (__brkval == 0 ? &__heap_start : __brkval);
  *reinterpret_cast<volatile uint32_t*>(heap_ptr) = STACK_CANARY_VALUE;
#elif defined(ARDUINO_ARCH_SAMD)
  if (!s_samd_stack_painted) {
    paintSamdFreeStack();
  }
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

StackWatermark getStackWatermark() {
#if defined(ARDUINO_ARCH_SAMD)
  const uintptr_t bss_end = reinterpret_cast<uintptr_t>(&__bss_end__);
  const uintptr_t stack_top = reinterpret_cast<uintptr_t>(&__StackTop);
  if (!s_samd_stack_painted || stack_top <= bss_end) {
    return {0U, 0U};
  }
  const uint32_t budget = static_cast<uint32_t>(stack_top - bss_end);
  const uint8_t* const start = reinterpret_cast<const uint8_t*>(bss_end);
  const uint8_t* const top = reinterpret_cast<const uint8_t*>(stack_top);
  const uint8_t* const it = etl::find_if(
      start, top, [](uint8_t b) { return b != kSamdStackPaintByte; });
  const uint32_t untouched = static_cast<uint32_t>(it - start);
  const uint32_t used = (budget >= untouched) ? (budget - untouched) : budget;
  return {used, budget};
#elif defined(ARDUINO_ARCH_ESP32)
  const uint32_t budget = static_cast<uint32_t>(getArduinoLoopTaskStackSize());
  const uint32_t min_free =
      static_cast<uint32_t>(uxTaskGetStackHighWaterMark(nullptr));
  if (budget == 0U) {
    return {0U, 0U};
  }
  const uint32_t used = (budget >= min_free) ? (budget - min_free) : budget;
  return {used, budget};
#else
  return {0U, 0U};
#endif
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
                     [pat](uint8_t v) { return v == pat; }))
      return false;
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

#if defined(ARDUINO_ARCH_AVR) || defined(ARDUINO_ARCH_SAMD)
namespace {
// Boot epoch kept as two persisted copies (ADR-0005). The newest value is the
// larger one; the increment overwrites the older copy, so a write torn by a
// power loss leaves the other copy intact and the next boot still moves
// forward. Erased EEPROM/flash reads as all ones and counts as zero. The write
// completes before any nonce of this epoch is emitted.
struct LinkEpochUpdate {
  uint64_t next;
  bool write_slot_a;
};

LinkEpochUpdate next_link_epoch(uint64_t slot_a, uint64_t slot_b) {
  if (slot_a == UINT64_MAX) slot_a = 0U;
  if (slot_b == UINT64_MAX) slot_b = 0U;
  return {etl::max(slot_a, slot_b) + 1U, slot_a <= slot_b};
}
}  // namespace
#endif

#if defined(ARDUINO_ARCH_AVR)
uint64_t linkBootEpoch() {
  // Both copies live in the EEPROM tail reserved by the pw_transfer resource.
  constexpr int kSlotA = static_cast<int>(
      E2END + 1U - bridge::config::LINK_EPOCH_EEPROM_RESERVED_BYTES);
  constexpr int kSlotB = kSlotA + static_cast<int>(sizeof(uint64_t));
  uint64_t slot_a = 0U;
  uint64_t slot_b = 0U;
  EEPROM.get(kSlotA, slot_a);
  EEPROM.get(kSlotB, slot_b);
  const LinkEpochUpdate update = next_link_epoch(slot_a, slot_b);
  EEPROM.put(update.write_slot_a ? kSlotA : kSlotB, update.next);
  return update.next;
}
#elif defined(ARDUINO_ARCH_SAMD)
// One NVM row per copy, so erasing one row never touches the other copy.
// Re-flashing the sketch resets both rows (see ADR-0005 limitations).
FlashStorage(link_epoch_slot_a, uint64_t);
FlashStorage(link_epoch_slot_b, uint64_t);

uint64_t linkBootEpoch() {
  const LinkEpochUpdate update =
      next_link_epoch(link_epoch_slot_a.read(), link_epoch_slot_b.read());
  if (update.write_slot_a) {
    link_epoch_slot_a.write(update.next);
  } else {
    link_epoch_slot_b.write(update.next);
  }
  return update.next;
}
#else
// Host builds have no persistent store; nonces are unique within a run.
__attribute__((weak)) uint64_t linkBootEpoch() { return 0U; }
#endif

bool fillLinkNonce(pw::span<const uint8_t> secret, pw::span<uint8_t> out) {
  if (out.size() != rpc::RPC_HANDSHAKE_NONCE_LENGTH) return false;
#if defined(ARDUINO_ARCH_ESP32)
  (void)secret;
  esp_fill_random(out.data(), out.size());
  return true;
#else
  static bool epoch_loaded = false;
  static uint64_t epoch = 0U;
  static uint64_t handshakes = 0U;
  if (!epoch_loaded) {
    epoch = linkBootEpoch();
    epoch_loaded = true;
  }
  static_assert(rpc::RPC_HANDSHAKE_NONCE_LENGTH == sizeof(uint64_t) * 2U);
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> counter_block = {};
  pw::bytes::CopyInOrder(pw::endian::big, epoch, counter_block.data());
  pw::bytes::CopyInOrder(pw::endian::big, handshakes,
                         counter_block.data() + sizeof(uint64_t));
  ++handshakes;
  return rpc::security::derive_mcu_nonce(secret, counter_block, out);
#endif
}

__attribute__((weak)) bool hasSD() { return false; }

__attribute__((weak)) pw::Status removeFile(etl::string_view) {
  return pw::Status::Unimplemented();
}

void applySafetyPin(uint8_t pin, SafetyPinState state) {
  if (pin >= NUM_DIGITAL_PINS) return;
#if defined(ARDUINO_ARCH_ESP32)
  if (!digitalPinIsValid(pin)) return;
#endif
  if (state == SafetyPinState::SAFE_PIN_LOW ||
      state == SafetyPinState::SAFE_PIN_HIGH) {
#if defined(ARDUINO_ARCH_ESP32)
    if (!digitalPinCanOutput(pin)) return;
#endif
    pw::digital_io::ArduinoDigitalOut output(
        pin, state == SafetyPinState::SAFE_PIN_HIGH
                 ? pw::digital_io::State::kActive
                 : pw::digital_io::State::kInactive);
    (void)output.Enable();
  } else {
    pw::digital_io::ArduinoDigitalIn input(
        pin, state == SafetyPinState::SAFE_PIN_INPUT_PULLUP);
    (void)input.Enable();
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
  const size_t len =
      etl::min(sizeof(board_name) - 1, sizeof(caps.board_name) - 1);
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
