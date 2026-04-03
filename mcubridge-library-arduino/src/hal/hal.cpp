#include "hal.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"
#include <etl/bitset.h>
#include <etl/string.h>
#include <etl/to_string.h>
#include <etl/binary.h>

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

namespace bridge::hal {

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
  if (path == nullptr || path[0] == rpc::RPC_NULL_TERMINATOR || path[0] == '/') {
    return false;
  }

  etl::string_view p(path);
  if (p.find('\\') != etl::string_view::npos) {
    return false;
  }
  if (p.find("..") != etl::string_view::npos) {
    return false;
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
    return false; // GCOVR_EXCL_LINE — internal: resolve_host_path catches empty paths first
  }

  path_buffer = full_path;

  for (size_t index = kHostFilesystemRootLength + 1U; index < full_path_length; ++index) {
    if (path_buffer[index] != '/') {
      continue;
    }
    char original = path_buffer[index];
    path_buffer[index] = rpc::RPC_NULL_TERMINATOR;
    if (!ensure_host_directory(path_buffer.c_str())) {
      return false; // GCOVR_EXCL_LINE — requires mkdir permission failure
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

void forceSafeState() {
  // [SIL-2] Ensure all potential actuator pins are in a safe state before any logic starts.
  // This prevents spikes or unintended activations during MCU boot/reset.
  for (uint8_t pin = 0; pin < DIGITAL_PINS; ++pin) {
    if constexpr (bridge::config::SAFE_START_PINS_ENABLED) {
      pinMode(pin, OUTPUT);
      digitalWrite(pin, LOW);
    } else {
      // Default: Using INPUT_PULLUP ensures pins are in a well-defined high-impedance state.
      pinMode(pin, INPUT_PULLUP);
    }
  }
}

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  int v;
  return static_cast<uint16_t>(reinterpret_cast<uintptr_t>(&v) - (__brkval == 0 ? reinterpret_cast<uintptr_t>(&__heap_start) : reinterpret_cast<uintptr_t>(__brkval)));
#elif defined(ARDUINO_ARCH_ESP32)
  return static_cast<uint16_t>(ESP.getFreeHeap());
#else
  return bridge::config::FALLBACK_FREE_MEMORY;
#endif
}

void init() {
  // [SIL-2] Force all digital pins to a safe state (Input with Pullups) on boot
  // to avoid floating states or accidental actuator activation.
  forceSafeState();

  if constexpr (bridge::config::ENABLE_WATCHDOG) {
#if defined(ARDUINO_ARCH_AVR)
    wdt_enable(WDTO_4S);
#elif defined(ARDUINO_ARCH_ESP32)
    esp_task_wdt_init(4, true);
    esp_task_wdt_add(nullptr);
#elif defined(ARDUINO_ARCH_ESP8266)
    ESP.wdtEnable(4000);
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

etl::expected<void, HalError> writeFile(etl::string_view path, etl::span<const uint8_t> data) {
#if defined(BRIDGE_HOST_TEST)
  // [SIL-2] Ensure null-termination for POSIX functions
  PathString rel_path;
  rel_path.assign(path.begin(), path.end());

  PathString full_path;
  if (!resolve_host_path(rel_path.c_str(), full_path) ||
      !ensure_host_parent_directories(full_path)) {
    return etl::unexpected<HalError>(HalError::IO_ERROR);
  }

  FILE* file = fopen(full_path.c_str(), "wb");
  if (file == nullptr) {
    return etl::unexpected<HalError>(HalError::IO_ERROR); // GCOVR_EXCL_LINE — requires filesystem-level failure
  }

  const size_t bytes_written = fwrite(data.data(), 1U, data.size(), file);
  const int flush_status = fflush(file);
  const int close_status = fclose(file);
  if ((bytes_written == data.size()) && (flush_status == 0) && (close_status == 0)) {
    return {};
  }
  return etl::unexpected<HalError>(HalError::IO_ERROR); // GCOVR_EXCL_LINE — requires write/flush/close failure
#else
  // [SIL-2] Real hardware SD implementation would go here.
  (void)path; (void)data;
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
#endif
}

etl::expected<ChunkResult, HalError> readFileChunk(
    etl::string_view path,
    size_t offset,
    etl::span<uint8_t> buffer) {
#if defined(BRIDGE_HOST_TEST)
  // [SIL-2] Ensure null-termination
  PathString rel_path;
  rel_path.assign(path.begin(), path.end());

  PathString full_path;
  if (!resolve_host_path(rel_path.c_str(), full_path)) {
    return etl::unexpected<HalError>(HalError::INVALID_ARGUMENT);
  }

  struct stat stat_buffer = {};
  if ((::stat(full_path.c_str(), &stat_buffer) != 0) || !S_ISREG(stat_buffer.st_mode)) {
    return etl::unexpected<HalError>(HalError::NOT_FOUND);
  }

  const size_t file_size = static_cast<size_t>(stat_buffer.st_size);
  if (offset > file_size) {
    return etl::unexpected<HalError>(HalError::INVALID_ARGUMENT);
  }

  FILE* file = fopen(full_path.c_str(), "rb");
  if (file == nullptr) {
    return etl::unexpected<HalError>(HalError::IO_ERROR); // GCOVR_EXCL_LINE — requires filesystem-level failure
  }

  if ((offset > 0U) && (fseek(file, static_cast<long>(offset), SEEK_SET) != 0)) {
    fclose(file); // GCOVR_EXCL_LINE — requires fseek failure
    return etl::unexpected<HalError>(HalError::IO_ERROR); // GCOVR_EXCL_LINE — requires fseek failure
  }

  ChunkResult result = {};
  result.bytes_read = fread(buffer.data(), 1U, buffer.size(), file);
  const bool read_failed = ferror(file) != 0;
  const int close_status = fclose(file);
  if (read_failed || (close_status != 0)) {
    return etl::unexpected<HalError>(HalError::IO_ERROR); // GCOVR_EXCL_LINE — requires ferror/fclose failure
  }

  result.has_more = (offset + result.bytes_read) < file_size;
  return result;
#else
  // [SIL-2] Real hardware SD implementation would go here.
  (void)path; (void)offset; (void)buffer;
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
#endif
}

etl::expected<void, HalError> removeFile(etl::string_view path) {
#if defined(BRIDGE_HOST_TEST)
  // [SIL-2] Ensure null-termination
  PathString rel_path;
  rel_path.assign(path.begin(), path.end());

  PathString full_path;
  if (!resolve_host_path(rel_path.c_str(), full_path)) {
    return etl::unexpected<HalError>(HalError::INVALID_ARGUMENT);
  }
  if (::unlink(full_path.c_str()) == 0) {
    return {};
  }
  return etl::unexpected<HalError>(HalError::IO_ERROR);
#else
  (void)path;
  return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
#endif
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
  if (hasSD()) {
    caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_SD));
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

}  // namespace bridge::hal

