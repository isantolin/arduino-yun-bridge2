#include "hal/hal.h"
#include "protocol/rpc_protocol.h"
#include <etl/string.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

namespace bridge {
namespace hal {

constexpr char kHostFilesystemRoot[] = "/tmp/mcubridge-host-fs";
constexpr size_t kHostFilesystemRootLength = sizeof(kHostFilesystemRoot) - 1U;
constexpr size_t kHostFilesystemPathCapacity = kHostFilesystemRootLength + rpc::RPC_MAX_FILEPATH_LENGTH + 2U;
using PathString = etl::string<kHostFilesystemPathCapacity>;

static bool resolve_to_full_path(etl::string_view path, PathString& full_path) {
  if (path.empty() || path.front() == '/' || path.find("..") != etl::string_view::npos) return false;
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

  size_t pos = 1;
  while ((pos = parent_dir.find('/', pos)) != etl::string_view::npos) {
    parent_dir[pos] = '\0';
    if (::mkdir(parent_dir.c_str(), 0755) != 0 && errno != EEXIST) return false;
    parent_dir[pos] = '/';
    pos++;
  }
  if (::mkdir(parent_dir.c_str(), 0755) != 0 && errno != EEXIST) return false;
  return true;
}

bool hasSD() { return true; }

etl::expected<void, HalError> writeFile(etl::string_view path, etl::span<const uint8_t> data) {
  PathString full_path;
  if (!resolve_to_full_path(path, full_path) || !ensure_host_parent_directories(full_path)) return etl::unexpected<HalError>(HalError::IO_ERROR);
  FILE* file = fopen(full_path.c_str(), "wb");
  if (file == nullptr) return etl::unexpected<HalError>(HalError::IO_ERROR);
  const size_t bytes_written = fwrite(data.data(), 1U, data.size(), file);
  fflush(file); fclose(file);
  return (bytes_written == data.size()) ? etl::expected<void, HalError>{} : etl::unexpected<HalError>(HalError::IO_ERROR);
}

etl::expected<ChunkResult, HalError> readFileChunk(etl::string_view path, size_t offset, etl::span<uint8_t> buffer) {
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
}

etl::expected<void, HalError> removeFile(etl::string_view path) {
  PathString full_path;
  if (!resolve_to_full_path(path, full_path)) return etl::unexpected<HalError>(HalError::INVALID_ARGUMENT);
  return (::unlink(full_path.c_str()) == 0) ? etl::expected<void, HalError>{} : etl::unexpected<HalError>(HalError::IO_ERROR);
}

} // namespace hal
} // namespace bridge