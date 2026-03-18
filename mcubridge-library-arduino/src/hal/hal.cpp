#include "hal.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"
#include "etl/bitset.h"

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
extern "C" {
  extern char *__brkval;
  extern char __heap_start;
}
#endif

namespace bridge {
namespace hal {

namespace {
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

bool resolve_host_path(const char* relative_path, char* output, size_t output_size) {
  if (!is_host_path_safe(relative_path) || !ensure_host_directory(kHostFilesystemRoot)) {
    return false;
  }

  const int written = snprintf(
      output,
      output_size,
      "%s/%s",
      kHostFilesystemRoot,
      relative_path);
  return (written > 0) && (static_cast<size_t>(written) < output_size);
}

bool ensure_host_parent_directories(const char* full_path) {
  char path_buffer[kHostFilesystemPathCapacity] = {};
  const size_t max_length = sizeof(path_buffer) - 1U;
  const size_t full_path_length = strnlen(full_path, max_length);

  if ((full_path_length == 0U) || (full_path_length >= max_length)) {
    return false;
  }

  memcpy(path_buffer, full_path, full_path_length);
  path_buffer[full_path_length] = '\0';

  for (size_t index = kHostFilesystemRootLength + 1U; index < full_path_length; ++index) {
    if (path_buffer[index] != '/') {
      continue;
    }
    path_buffer[index] = '\0';
    if (!ensure_host_directory(path_buffer)) {
      return false;
    }
    path_buffer[index] = '/';
  }
  return true;
}
#endif
}

bool isValidPin(uint8_t pin) {
#if defined(BRIDGE_HOST_TEST)
  (void)pin;
  return true; // Always allow in host tests/emulator
#elif defined(NUM_DIGITAL_PINS)
  return pin < NUM_DIGITAL_PINS;
#else
  return pin <= bridge::config::FALLBACK_MAX_PIN;
#endif
}

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  int v;
  return static_cast<uint16_t>(reinterpret_cast<int>(&v) - (__brkval == 0 ? reinterpret_cast<int>(&__heap_start) : reinterpret_cast<int>(__brkval)));
#elif defined(ARDUINO_ARCH_ESP32)
  return (uint16_t)ESP.getFreeHeap();
#else
  return bridge::config::FALLBACK_FREE_MEMORY;
#endif
}

void init() {
#if defined(ARDUINO_ARCH_AVR)
  // Enable watchdog or other AVR-specific init
#endif
}

bool hasSD() {
#if defined(BRIDGE_HOST_TEST)
  return true; // Mock SD card availability for tests
#else
  // For actual hardware, this would check SD.begin() or similar.
  // Since we don't want to force SD.h dependency here, we return false
  // unless specifically enabled by a build flag.
  return false;
#endif
}

bool writeFile(const char* path, etl::span<const uint8_t> data) {
#if defined(BRIDGE_HOST_TEST)
  char full_path[kHostFilesystemPathCapacity] = {};
  if (!resolve_host_path(path, full_path, sizeof(full_path)) ||
      !ensure_host_parent_directories(full_path)) {
    return false;
  }

  FILE* file = fopen(full_path, "wb");
  if (file == nullptr) {
    return false;
  }

  const size_t bytes_written = fwrite(data.data(), 1U, data.size(), file);
  const int flush_status = fflush(file);
  const int close_status = fclose(file);
  return (bytes_written == data.size()) && (flush_status == 0) && (close_status == 0);
#else
  (void)path; (void)data;
  return false;
#endif
}

bool readFileChunk(
    const char* path,
    size_t offset,
    etl::span<uint8_t> buffer,
    size_t& bytes_read,
    bool& has_more) {
  bytes_read = 0U;
  has_more = false;

#if defined(BRIDGE_HOST_TEST)
  char full_path[kHostFilesystemPathCapacity] = {};
  if (!resolve_host_path(path, full_path, sizeof(full_path))) {
    return false;
  }

  struct stat stat_buffer = {};
  if ((::stat(full_path, &stat_buffer) != 0) || !S_ISREG(stat_buffer.st_mode)) {
    return false;
  }

  const size_t file_size = static_cast<size_t>(stat_buffer.st_size);
  if (offset > file_size) {
    return false;
  }

  FILE* file = fopen(full_path, "rb");
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
  (void)path;
  (void)offset;
  (void)buffer;
  return false;
#endif
}

bool removeFile(const char* path) {
#if defined(BRIDGE_HOST_TEST)
  char full_path[kHostFilesystemPathCapacity] = {};
  if (!resolve_host_path(path, full_path, sizeof(full_path))) {
    return false;
  }
  return ::unlink(full_path) == 0;
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
  return static_cast<uint32_t>(caps.to_ulong());
}

void getPinCounts(uint8_t& digital, uint8_t& analog) {
#if defined(BRIDGE_HOST_TEST)
  digital = bridge::config::SAMD_DIGITAL_PINS;
  analog = bridge::config::SAMD_ANALOG_PINS;
#elif defined(ARDUINO_ARCH_AVR)
  digital = bridge::config::AVR_DIGITAL_PINS;
  analog = bridge::config::AVR_ANALOG_PINS;
#elif defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM)
  digital = bridge::config::SAMD_DIGITAL_PINS;
  analog = bridge::config::SAMD_ANALOG_PINS;
#else
  digital = bridge::config::FALLBACK_MAX_PIN;
  analog = 0;
#endif
}

}  // namespace hal
}  // namespace bridge
