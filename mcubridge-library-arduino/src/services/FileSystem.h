#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include <etl/string_view.h>
#include <pw_span/span.h>

#include "config/bridge_config.h"
#include "protocol/mcu_wire.pb.h"

#include <pw_status/status.h>
#include <pw_transfer/transfer.h>

class FileSystemClass {
 public:
  FileSystemClass() = default;
  static void write(etl::string_view path, pw::span<const uint8_t> data);
  static void remove(etl::string_view path);

  static void _onWrite(const rpc_pb_FileWrite& msg);
  static void _onRemove(const rpc_pb_FileRemove& msg);

  // ADR-0007 (E6): store `data` at `path` under the MPU file_system_root via
  // pw_transfer. The bytes are exposed read-only on FILE_UPLOAD_RESOURCE_ID
  // and must stay valid and unchanged until uploadPending() returns false.
  // Returns false when an upload is already running, the link is not ready,
  // or the arguments are invalid. One upload runs at a time.
  static bool upload(etl::string_view path, pw::span<const uint8_t> data);
  static bool uploadPending();
  // Status reported by the daemon for the last finished upload.
  static pw::Status lastUploadStatus();
  static bool registerUploadHandler(pw::transfer::TransferService& service);
  static void _resetUpload();
};

using FileSystemType = FileSystemClass;
extern FileSystemType FileSystem;

#endif
