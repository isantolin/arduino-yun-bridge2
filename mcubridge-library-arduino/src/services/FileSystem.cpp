#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include "services/FileSystem.h"

#include <etl/algorithm.h>
#include <etl/string_view.h>
#include <pw_span/span.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

void FileSystemClass::write(etl::string_view path,
                            pw::span<const uint8_t> data) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }
  rpc_pb_FileWrite p = rpc_pb_FileWrite_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);
  bridge::pb::copy_bytes(p.data, data);

  (void)Bridge.fileSystemClient().Write(p);
}

void FileSystemClass::remove(etl::string_view path) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }
  rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  (void)Bridge.fileSystemClient().Remove(p);
}

void FileSystemClass::_onWrite(const rpc_pb_FileWrite& msg) {
  bridge::hal::writeFile(
      etl::string_view(msg.path),
      pw::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

void FileSystemClass::_onRemove(const rpc_pb_FileRemove& msg) {
  bridge::hal::removeFile(etl::string_view(msg.path));
}

FileSystemType FileSystem;

#endif
