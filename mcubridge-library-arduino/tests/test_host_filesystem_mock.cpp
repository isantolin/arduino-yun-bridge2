#include <errno.h>
#include <etl/string.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "BridgeFaultInjection.h"
#include "config/bridge_config.h"
#include "hal/hal.h"
#include "protocol/rpc_protocol.h"

bool g_host_has_sd = true;
bool g_host_fs_enabled = true;

namespace bridge {
namespace hal {

constexpr char kHostFilesystemRoot[] = "/tmp/mcubridge-host-fs";
constexpr size_t kHostFilesystemRootLength = sizeof(kHostFilesystemRoot) - 1U;
constexpr size_t kHostFilesystemPathCapacity =
    kHostFilesystemRootLength + rpc::RPC_MAX_FILEPATH_LENGTH + 2U;
using PathString = etl::string<kHostFilesystemPathCapacity>;

static bool resolve_to_full_path(etl::string_view path, PathString& full_path) {
  if (path.empty() || path.front() == '/' ||
      path.find("..") != etl::string_view::npos)
    return false;
  full_path.assign(kHostFilesystemRoot);
  full_path.append("/");
  full_path.append(path.data(), path.length());
  return true;
}

static bool ensure_host_parent_directories(const PathString& full_path) {
  PathString parent_dir = full_path;
  size_t last_slash = parent_dir.rfind('/');
  if (last_slash == etl::string_view::npos) return false;
  parent_dir.resize(last_slash);

  struct stat st = {};
  if (::stat(parent_dir.c_str(), &st) == 0) return S_ISDIR(st.st_mode);

  bool mkdir_ok = true;
  etl::for_each(parent_dir.begin() + 1, parent_dir.end(), [&](char& ch) {
    if (mkdir_ok && ch == '/') {
      ch = '\0';
      if (::mkdir(parent_dir.c_str(), 0755) != 0 && errno != EEXIST) {
        mkdir_ok = false;
      }
      ch = '/';
    }
  });
  if (!mkdir_ok) return false;
  if (::mkdir(parent_dir.c_str(), 0755) != 0 && errno != EEXIST) return false;
  return true;
}

bool hasSD() { return g_host_fs_enabled && g_host_has_sd; }

pw::Status writeFile(etl::string_view path, pw::span<const uint8_t> data) {
  if (!g_host_fs_enabled) return pw::Status::Unimplemented();
  PathString full_path;
  if (!resolve_to_full_path(path, full_path) ||
      !ensure_host_parent_directories(full_path))
    return pw::Status::Internal();
  FILE* file = fopen(full_path.c_str(), "wb");
  if (file == nullptr) return pw::Status::Internal();
  const size_t bytes_written = fwrite(data.data(), 1U, data.size(), file);
  fflush(file);
  fclose(file);
  return (bytes_written == data.size()) ? pw::OkStatus()
                                        : pw::Status::Internal();
}

pw::Result<ChunkResult> readFileChunk(etl::string_view path, size_t offset,
                                      pw::span<uint8_t> buffer) {
  if (!g_host_fs_enabled) return pw::Status::Unimplemented();
  PathString full_path;
  if (!resolve_to_full_path(path, full_path))
    return pw::Status::InvalidArgument();
  struct stat st = {};
  if ((::stat(full_path.c_str(), &st) != 0) || !S_ISREG(st.st_mode))
    return pw::Status::NotFound();
  const size_t file_size = static_cast<size_t>(st.st_size);
  if (offset > file_size) return pw::Status::InvalidArgument();
  FILE* file = fopen(full_path.c_str(), "rb");
  if (file == nullptr) return pw::Status::Internal();
  if ((offset > 0U) &&
      (fseek(file, static_cast<long>(offset), SEEK_SET) != 0)) {
    fclose(file);
    return pw::Status::Internal();
  }
  ChunkResult result = {};
  result.bytes_read = fread(buffer.data(), 1U, buffer.size(), file);
  bool failed = ferror(file) != 0;
  fclose(file);
  if (failed) return pw::Status::Internal();
  result.has_more = (offset + result.bytes_read) < file_size;
  if (bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT)) {
    bridge::test::fault::advance_clock_ms(bridge::config::SERIAL_TIMEOUT_MS +
                                          1U);
    result.has_more = true;
  }
  return result;
}

pw::Status removeFile(etl::string_view path) {
  if (!g_host_fs_enabled) return pw::Status::Unimplemented();
  PathString full_path;
  if (!resolve_to_full_path(path, full_path))
    return pw::Status::InvalidArgument();
  return (::unlink(full_path.c_str()) == 0) ? pw::OkStatus()
                                            : pw::Status::Internal();
}

}  // namespace hal
}  // namespace bridge
