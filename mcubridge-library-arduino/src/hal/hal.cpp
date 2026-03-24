#include "hal.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"
#include <etl/bitset.h>
#include <etl/string.h>
#include <etl/to_string.h>

#if defined(BRIDGE_HOST_TEST)
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

#if defined(ARDUINO_ARCH_AVR)
#include <avr/io.h>
#include <avr/wdt.h>
extern "C" {
  extern char *__brkval;
  extern char __heap_start;
}
#elif defined(ARDUINO_ARCH_ESP32)
#include <esp_task_wdt.h>
#elif defined(ARDUINO_ARCH_ESP8266)
#include <Arduino.h>
#endif

namespace bridge {
namespace hal {

namespace {
// [SIL-2] Use constexpr for compile-time architecture identification
#if defined(BRIDGE_HOST_TEST)
constexpr uint8_t CURRENT_ARCH = rpc::RPC_ARCH_SAMD;
constexpr uint8_t DIGITAL_PINS = bridge::config::SAMD_DIGITAL_PINS;
constexpr uint8_t ANALOG_PINS = bridge::config::SAMD_ANALOG_PINS;
#elif defined(ARDUINO_ARCH_AVR)
constexpr uint8_t CURRENT_ARCH = rpc::RPC_ARCH_AVR;
constexpr uint8_t DIGITAL_PINS = bridge::config::AVR_DIGITAL_PINS;
constexpr uint8_t ANALOG_PINS = bridge::config::AVR_ANALOG_PINS;
#elif defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM)
constexpr uint8_t CURRENT_ARCH = rpc::RPC_ARCH_SAMD;
constexpr uint8_t DIGITAL_PINS = bridge::config::SAMD_DIGITAL_PINS;
constexpr uint8_t ANALOG_PINS = bridge::config::SAMD_ANALOG_PINS;
#else
constexpr uint8_t CURRENT_ARCH = 0; // Unknown
constexpr uint8_t DIGITAL_PINS = bridge::config::FALLBACK_MAX_PIN;
constexpr uint8_t ANALOG_PINS = 0;
#endif

constexpr uint8_t bit_index_from_mask(uint32_t mask) {
  uint8_t bit_index = 0;
  while (mask > 1U) {
    mask /= 2U;
    ++bit_index;
  }
  return bit_index;
}

#if defined(BRIDGE_HOST_TEST)
constexpr char kHostFilesystemRoot[] = "/tmp/mcubridge-host-fs";
constexpr size_t kHostFilesystemRootLength = sizeof(kHostFilesystemRoot) - 1U;
constexpr size_t kHostFilesystemPathCapacity =
    kHostFilesystemRootLength + rpc::RPC_MAX_FILEPATH_LENGTH + 2U;

using PathString = etl::string<kHostFilesystemPathCapacity>;

bool ensure_host_directory(const char* path) {
  if (::mkdir(path, 0700) == 0) {
    return true;
  }
  return errno == EEXIST;
}

bool is_host_path_safe(const char* path) {
  if ((path == nullptr) || (path[0] == '\0') || (path[0] == '/')) {
    return false;
  }

  for (size_t index = 0; path[index] != '\0'; ++index) {
    if (path[index] == '\\') {
      return false;
    }
    if ((path[index] == '.') && (path[index + 1] == '.')) {
      return false;
    }
  }
  return true;
}

bool resolve_host_path(const char* relative_path, PathString& output) {
  if (!is_host_path_safe(relative_path) || !ensure_host_directory(kHostFilesystemRoot)) {
    return false;
  }

  output.assign(kHostFilesystemRoot);
  output.append("/");
  output.append(relative_path);
  return true;
}

bool ensure_host_parent_directories(const PathString& full_path) {
  PathString path_buffer;
  const size_t full_path_length = full_path.size();

  if (full_path_length == 0U) {
    return false;
  }

  path_buffer = full_path;

  for (size_t index = kHostFilesystemRootLength + 1U; index < full_path_length; ++index) {
    if (path_buffer[index] != '/') {
      continue;
    }
    char original = path_buffer[index];
    path_buffer[index] = '\0';
    if (!ensure_host_directory(path_buffer.c_str())) {
      return false;
    }
    path_buffer[index] = original;
  }
  return true;
}
#endif
}

bool isValidPin(const uint8_t pin) {
  return pin < DIGITAL_PINS;
}

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  int v;
  return static_cast<uint16_t>(reinterpret_cast<int>(&v) - (__brkval == 0 ? reinterpret_cast<int>(&__heap_start) : reinterpret_cast<int>(__brkval)));
#elif defined(ARDUINO_ARCH_ESP32)
  return static_cast<uint16_t>(ESP.getFreeHeap());
#else
  return bridge::config::FALLBACK_FREE_MEMORY;
#endif
}

void init() {
  // [SIL-2] Force all digital pins to a safe state (Input with Pullups) on boot
  // to avoid floating states or accidental actuator activation.
  for (uint8_t pin = 0; pin < DIGITAL_PINS; ++pin) {
    pinMode(pin, INPUT_PULLUP);
  }

  if constexpr (bridge::config::BRIDGE_ENABLE_WATCHDOG) {
#if defined(ARDUINO_ARCH_AVR)
    wdt_enable(WDTO_2S);
#elif defined(ARDUINO_ARCH_ESP32)
    esp_task_wdt_init(2, true);
    esp_task_wdt_add(NULL);
#elif defined(ARDUINO_ARCH_ESP8266)
    ESP.wdtEnable(2000);
#elif defined(ARDUINO_ARCH_SAMD)
    // SAMD WDT initialization is usually board-specific; ensure generic safety.
#endif
  }
}

bool hasSD() {
#if defined(BRIDGE_HOST_TEST)
  return true; // Mock SD card availability for tests
#else
  return false;
#endif
}

bool writeFile(etl::string_view path, etl::span<const uint8_t> data) {
#if defined(BRIDGE_HOST_TEST)
  PathString full_path;
  if (!resolve_host_path(path.data(), full_path) ||
      !ensure_host_parent_directories(full_path)) {
    return false;
  }

  FILE* file = fopen(full_path.c_str(), "wb");
  if (file == nullptr) {
    return false;
  }

  const size_t bytes_written = fwrite(data.data(), 1U, data.size(), file);
  const int flush_status = fflush(file);
  const int close_status = fclose(file);
  return (bytes_written == data.size()) && (flush_status == 0) && (close_status == 0);
#else
  // [SIL-2] Real hardware SD implementation would go here.
  // Returning false ensures the Service emits STATUS_ERROR to Linux.
  (void)path; (void)data;
  return false;
#endif
}

bool readFileChunk(
    etl::string_view path,
    size_t offset,
    etl::span<uint8_t> buffer,
    size_t& bytes_read,
    bool& has_more) {
  bytes_read = 0U;
  has_more = false;

#if defined(BRIDGE_HOST_TEST)
  PathString full_path;
  if (!resolve_host_path(path.data(), full_path)) {
    return false;
  }

  struct stat stat_buffer = {};
  if ((::stat(full_path.c_str(), &stat_buffer) != 0) || !S_ISREG(stat_buffer.st_mode)) {
    return false;
  }

  const size_t file_size = static_cast<size_t>(stat_buffer.st_size);
  if (offset > file_size) {
    return false;
  }

  FILE* file = fopen(full_path.c_str(), "rb");
  if (file == nullptr) {
    return false;
  }

  if ((offset > 0U) && (fseek(file, static_cast<long>(offset), SEEK_SET) != 0)) {
    fclose(file);
    return false;
  }

  bytes_read = fread(buffer.data(), 1U, buffer.size(), file);
  const bool read_failed = ferror(file) != 0;
  const int close_status = fclose(file);
  if (read_failed || (close_status != 0)) {
    return false;
  }

  has_more = (offset + bytes_read) < file_size;
  return true;
#else
  // [SIL-2] Real hardware SD implementation would go here.
  (void)path; (void)offset; (void)buffer;
  return false;
#endif
}

bool removeFile(etl::string_view path) {
#if defined(BRIDGE_HOST_TEST)
  PathString full_path;
  if (!resolve_host_path(path.data(), full_path)) {
    return false;
  }
  return ::unlink(full_path.c_str()) == 0;
#else
  (void)path;
  return false;
#endif
}

uint32_t getCapabilities() {
  etl::bitset<32> caps;
#if BRIDGE_ENABLE_WATCHDOG
  caps.set(bit_index_from_mask(rpc::RPC_CAPABILITY_WATCHDOG));
#endif
#if BRIDGE_ENABLE_RLE
  caps.set(bit_index_from_mask(rpc::RPC_CAPABILITY_RLE));
#endif
#if defined(ARDUINO_ARCH_AVR) && defined(SERIAL_PORT_HARDWARE1)
  caps.set(bit_index_from_mask(rpc::RPC_CAPABILITY_HW_SERIAL1));
#endif
#if defined(BRIDGE_ENABLE_DAC)
  caps.set(bit_index_from_mask(rpc::RPC_CAPABILITY_DAC));
#endif
#if BRIDGE_ENABLE_I2C
  caps.set(bit_index_from_mask(rpc::RPC_CAPABILITY_I2C));
#endif
#if BRIDGE_ENABLE_SPI
  caps.set(bit_index_from_mask(rpc::RPC_CAPABILITY_SPI));
#endif
  if (hasSD()) {
    caps.set(bit_index_from_mask(rpc::RPC_CAPABILITY_SD));
  }
  return static_cast<uint32_t>(caps.to_ulong());
}

void getPinCounts(uint8_t& digital, uint8_t& analog) {
  digital = DIGITAL_PINS;
  analog = ANALOG_PINS;
}

uint8_t getArchId() {
  return CURRENT_ARCH;
}

}  // namespace hal
}  // namespace bridge

