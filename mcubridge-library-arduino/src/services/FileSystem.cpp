#include "FileSystem.h"
#include "Bridge.h"
#include "util/pb_copy.h"

#if BRIDGE_ENABLE_FILESYSTEM

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path, etl::span<const uint8_t> data) {
  Bridge.sendKeyDataCommand(rpc::CommandId::CMD_FILE_WRITE, path, &rpc::payload::FileWrite::path, data, &rpc::payload::FileWrite::data);
}

void FileSystemClass::read(etl::string_view path, FileSystemReadHandler handler) {
  if (Bridge.sendKeyCommand(rpc::CommandId::CMD_FILE_READ, path, &rpc::payload::FileRead::path)) {
    _read_handler = handler;
  }
}

void FileSystemClass::remove(etl::string_view path) {
  Bridge.sendKeyCommand(rpc::CommandId::CMD_FILE_REMOVE, path, &rpc::payload::FileRemove::path);
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg, etl::span<const uint8_t> data) {
  if (bridge::hal::hasSD()) {
    // [SIL-2] Use HAL abstraction to write data to SD card.
    if (bridge::hal::writeFile(msg.path, data)) {
      Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
    } else {
      Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
    }
  } else {
    // If no SD card present, signal error back to Linux daemon.
    Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::_onResponse(etl::span<const uint8_t> content) {
  if (_read_handler.is_valid()) {
    _read_handler(content);
  }
}
#endif
