#include "FileSystem.h"
#include "Bridge.h"
#include "util/pb_copy.h"

#if BRIDGE_ENABLE_FILESYSTEM

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path, etl::span<const uint8_t> data) {
  rpc::payload::FileWrite msg = {};
  rpc::util::pb_copy_string(path, msg.path, sizeof(msg.path));
  rpc::util::pb_setup_encode_span(msg.data, data);
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_WRITE, msg);
}

void FileSystemClass::read(etl::string_view path, FileSystemReadHandler handler) {
  rpc::payload::FileRead msg = {};
  rpc::util::pb_copy_string(path, msg.path, sizeof(msg.path));
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_READ, msg)) {
    _read_handler = handler;
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove msg = {};
  rpc::util::pb_copy_string(path, msg.path, sizeof(msg.path));
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_REMOVE, msg);
}

// [SIL-2] Intentional no-op: write-back to Linux is handled by the daemon.\nvoid FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) { (void)msg; }

void FileSystemClass::_onResponse(etl::span<const uint8_t> content) {
  if (_read_handler.is_valid()) {
    _read_handler(content);
  }
}
#endif
