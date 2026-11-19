#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include <etl/string.h>
#include <unistd.h>

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

bool hasSD() { return g_host_fs_enabled && g_host_has_sd; }

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
