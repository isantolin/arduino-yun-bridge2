#include "services/FileSystem.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

FileSystemClass::FileSystemClass() {}

void FileSystemClass::_onWrite(const rpc_pb_FileWrite& msg) {
  (void)bridge::hal::writeFile(msg.path,
                                etl::span<const uint8_t>(msg.data.bytes,
                                                         msg.data.size));
}

void FileSystemClass::_onRead(const rpc_pb_FileRead& msg) {
  uint8_t buffer[64];
  auto res = bridge::hal::readFileChunk(msg.path, 0, etl::span<uint8_t>(buffer, 64));
  if (res) {
    rpc_pb_FileReadResponse resp = rpc_pb_FileReadResponse_init_default;
    rpc::payload::copy_to_pb_bytes(resp.content, buffer, res->bytes_read);
    (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, rpc_pb_FileReadResponse_fields, resp);
  }
}

void FileSystemClass::_onRemove(const rpc_pb_FileRemove& msg) {
  (void)bridge::hal::removeFile(msg.path);
}

void FileSystemClass::_onResponse(const rpc_pb_FileReadResponse& msg) {
  if (_read_handler.is_valid()) {
    _read_handler(etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

void FileSystemClass::write(etl::string_view path, etl::span<const uint8_t> data) {
  rpc_pb_FileWrite p = rpc_pb_FileWrite_init_default;
  rpc::payload::copy_to_pb_string(p.path, path);
  rpc::payload::copy_to_pb_bytes(p.data, data.data(), data.size());
  (void)Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, rpc_pb_FileWrite_fields, p);
}

void FileSystemClass::read(etl::string_view path, FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc_pb_FileRead p = rpc_pb_FileRead_init_default;
  rpc::payload::copy_to_pb_string(p.path, path);
  (void)Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, rpc_pb_FileRead_fields, p);
}

void FileSystemClass::remove(etl::string_view path) {
  rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
  rpc::payload::copy_to_pb_string(p.path, path);
  (void)Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0, rpc_pb_FileRemove_fields, p);
}

FileSystemClass FileSystem;

#endif
