#include "hal.h"
#include "ArchTraits.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"
#include <etl/bitset.h>
#include <etl/string.h>
#include <etl/algorithm.h>
#include <etl/to_string.h>
#include <etl/binary.h>
#include <etl/iterator.h>

#if defined(BRIDGE_HOST_TEST)
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

#if defined(ARDUINO_ARCH_AVR)
extern "C" {
  extern char *__brkval;
  extern char __heap_start;
}
#endif

namespace bridge::hal {

namespace {
using Traits = CurrentArchTraits;

constexpr uint8_t CURRENT_ARCH = (Traits::id == ArchId::ARCH_ID_AVR) ? rpc::RPC_ARCH_AVR :
                                 (Traits::id == ArchId::ARCH_ID_HOST) ? rpc::RPC_ARCH_SAMD : 0;

constexpr uint8_t DIGITAL_PINS = (Traits::id == ArchId::ARCH_ID_AVR) ? static_cast<uint8_t>(bridge::config::DIGITAL_PINS) :
                                 (Traits::id == ArchId::ARCH_ID_HOST) ? static_cast<uint8_t>(bridge::config::SAMD_DIGITAL_PINS) :
                                 static_cast<uint8_t>(bridge::config::DIGITAL_PINS);

constexpr uint8_t ANALOG_PINS = (Traits::id == ArchId::ARCH_ID_AVR) ? static_cast<uint8_t>(bridge::config::ANALOG_PINS) :
                                (Traits::id == ArchId::ARCH_ID_HOST) ? static_cast<uint8_t>(bridge::config::SAMD_ANALOG_PINS) : 0;

#if defined(BRIDGE_HOST_TEST)
constexpr char kHostFilesystemRoot[] = "/tmp/mcubridge-host-fs";
constexpr size_t kHostFilesystemRootLength = sizeof(kHostFilesystemRoot) - 1U;
constexpr size_t kHostFilesystemPathCapacity = kHostFilesystemRootLength + rpc::RPC_MAX_FILEPATH_LENGTH + 2U;
using PathString = etl::string<kHostFilesystemPathCapacity>;

bool ensure_host_directory(const char* path) {
  if (::mkdir(path, 0700) == 0) return true;
  return errno == EEXIST;
}

bool is_host_path_safe(const char* path) {
  if (path == nullptr || path[0] == rpc::RPC_NULL_TERMINATOR || path[0] == '/') return false;
  etl::string_view p(path);
  return (p.find('\\') == etl::string_view::npos) && (p.find("..") == etl::string_view::npos);
}

bool resolve_host_path(const char* relative_path, PathString& output) {
  if (!is_host_path_safe(relative_path) || !ensure_host_directory(kHostFilesystemRoot)) return false;
  output.assign(kHostFilesystemRoot); output.append("/"); output.append(relative_path);
  return true;
}

bool ensure_host_parent_directories(const PathString& full_path) {
  if (full_path.empty()) return false;
  PathString path_buffer = full_path;
  auto it = path_buffer.begin() + kHostFilesystemRootLength + 1U;
  auto end = path_buffer.end();
  bool success = true;
  (void)etl::find_if(it, end, [&](char& c) {
    if (c == '/') {
      char original = c; c = rpc::RPC_NULL_TERMINATOR;
      if (!ensure_host_directory(path_buffer.c_str())) { success = false; return true; }
      c = original;
    }
    return false;
  });
  return success;
}

bool resolve_to_full_path(etl::string_view path, PathString& full_path_out) {
  PathString rel_path; rel_path.assign(path.begin(), path.end());
  return resolve_host_path(rel_path.c_str(), full_path_out);
}
#endif
}

bool isValidPin(const uint8_t pin) { return pin < DIGITAL_PINS; }

void _forceSafeStateRecursive(uint8_t pin, uint8_t count) {
  if (pin >= count) return;
  if constexpr (bridge::config::SAFE_START_PINS_ENABLED) {
    ::pinMode(pin, OUTPUT); 
    ::digitalWrite(pin, LOW);
  } else {
    ::pinMode(pin, INPUT_PULLUP);
  }
  _forceSafeStateRecursive(pin + 1, count);
}

void forceSafeState() {
  const uint8_t pin_count = (Traits::id == ArchId::ARCH_ID_AVR) ? 
                            static_cast<uint8_t>(bridge::config::DIGITAL_PINS) : 
                            static_cast<uint8_t>(bridge::config::SAMD_DIGITAL_PINS);

  _forceSafeStateRecursive(0, pin_count);
}

uint16_t getFreeMemory() {
  if constexpr (Traits::id == ArchId::ARCH_ID_AVR) {
#if defined(ARDUINO_ARCH_AVR)
    int v;
    return static_cast<uint16_t>(reinterpret_cast<uintptr_t>(&v) - (__brkval == 0 ? reinterpret_cast<uintptr_t>(&__heap_start) : reinterpret_cast<uintptr_t>(__brkval)));
#else
    return Traits::default_free_memory;
#endif
  } else if constexpr (Traits::id == ArchId::ARCH_ID_ESP32) {
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
    if constexpr (Traits::id == ArchId::ARCH_ID_AVR) {
#if defined(ARDUINO_ARCH_AVR)
      wdt_enable(WDTO_4S);
#endif
    } else if constexpr (Traits::id == ArchId::ARCH_ID_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
      esp_task_wdt_init(4, true); esp_task_wdt_add(nullptr);
#endif
    }
  }
}

bool hasSD() { return (Traits::id == ArchId::ARCH_ID_HOST); }

etl::expected<void, HalError> writeFile(etl::string_view path, etl::span<const uint8_t> data) {
#if defined(BRIDGE_HOST_TEST)
  PathString full_path;
  if (!resolve_to_full_path(path, full_path) || !ensure_host_parent_directories(full_path)) return etl::unexpected<HalError>(HalError::IO_ERROR);
  FILE* file = fopen(full_path.c_str(), "wb");
  if (file == nullptr) return etl::unexpected<HalError>(HalError::IO_ERROR);
  const size_t bytes_written = fwrite(data.data(), 1U, data.size(), file);
  fflush(file); fclose(file);
  return (bytes_written == data.size()) ? etl::expected<void, HalError>{} : etl::unexpected<HalError>(HalError::IO_ERROR);
#else
  (void)path; (void)data; return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
#endif
}

etl::expected<ChunkResult, HalError> readFileChunk(etl::string_view path, size_t offset, etl::span<uint8_t> buffer) {
#if defined(BRIDGE_HOST_TEST)
  PathString full_path;
  if (!resolve_to_full_path(path, full_path)) return etl::unexpected<HalError>(HalError::INVALID_ARGUMENT);
  struct stat st = {};
  if ((::stat(full_path.c_str(), &st) != 0) || !S_ISREG(st.st_mode)) return etl::unexpected<HalError>(HalError::NOT_FOUND);
  const size_t file_size = static_cast<size_t>(st.st_size);
  if (offset > file_size) return etl::unexpected<HalError>(HalError::INVALID_ARGUMENT);
  FILE* file = fopen(full_path.c_str(), "rb");
  if (file == nullptr) return etl::unexpected<HalError>(HalError::IO_ERROR);
  if ((offset > 0U) && (fseek(file, static_cast<long>(offset), SEEK_SET) != 0)) { fclose(file); return etl::unexpected<HalError>(HalError::IO_ERROR); }
  ChunkResult result = {};
  result.bytes_read = fread(buffer.data(), 1U, buffer.size(), file);
  bool failed = ferror(file) != 0; fclose(file);
  if (failed) return etl::unexpected<HalError>(HalError::IO_ERROR);
  result.has_more = (offset + result.bytes_read) < file_size;
  return result;
#else
  (void)path; (void)offset; (void)buffer; return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
#endif
}

etl::expected<void, HalError> removeFile(etl::string_view path) {
#if defined(BRIDGE_HOST_TEST)
  PathString full_path;
  if (!resolve_to_full_path(path, full_path)) return etl::unexpected<HalError>(HalError::INVALID_ARGUMENT);
  return (::unlink(full_path.c_str()) == 0) ? etl::expected<void, HalError>{} : etl::unexpected<HalError>(HalError::IO_ERROR);
#else
  (void)path; return etl::unexpected<HalError>(HalError::NOT_IMPLEMENTED);
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
  if (hasSD()) caps.set(etl::count_trailing_zeros(rpc::RPC_CAPABILITY_SD));
  return static_cast<uint32_t>(caps.to_ulong());
}

void getPinCounts(uint8_t& digital, uint8_t& analog) { digital = DIGITAL_PINS; analog = ANALOG_PINS; }
uint8_t getArchId() { return CURRENT_ARCH; }

}  // namespace bridge::hal
