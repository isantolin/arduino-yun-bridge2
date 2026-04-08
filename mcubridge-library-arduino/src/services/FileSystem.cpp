#include "services/FileSystem.h"
#include "Bridge.h"
#include "util/string_copy.h"

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = rpc::MAX_PAYLOAD_SIZE - 3U;

void send_read_response(etl::span<const uint8_t> content) {
  rpc::payload::FileReadResponse msg = {};
  msg.content = content;
  (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, msg);
}
}  // namespace

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path, etl::span<const uint8_t> data) {
  rpc::payload::FileWrite msg = {};
  msg.path = path;
  msg.data = data;
  (void)Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, msg);
}

void FileSystemClass::read(etl::string_view path, FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead msg = {};
  msg.path = path;
  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, msg)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove msg = {};
  msg.path = path;
  (void)Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0, msg);
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) {
  auto res = bridge::hal::writeFile(etl::string_view(msg.path.data(), msg.path.size()), msg.data);
  (void)Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK : rpc::StatusCode::STATUS_ERROR);
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  size_t offset = 0;
  uint8_t buffer[kReadChunkSize];
  uint32_t start_ms = bridge::now_ms();
  size_t chunk_count = 0;
  etl::string_view path(msg.path.data(), msg.path.size());

  while (chunk_count++ < bridge::config::FILE_MAX_READ_CHUNKS && (bridge::now_ms() - start_ms < bridge::config::SERIAL_TIMEOUT_MS)) {
    auto res = bridge::hal::readFileChunk(path, offset, etl::span<uint8_t>(buffer, kReadChunkSize));
    if (!res) {
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    send_read_response(etl::span<const uint8_t>(buffer, res->bytes_read));
    if (!res->has_more) {
      send_read_response(etl::span<const uint8_t>());
      return;
    }
    offset += res->bytes_read;
  }
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  auto res = bridge::hal::removeFile(etl::string_view(msg.path.data(), msg.path.size()));
  (void)Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK : rpc::StatusCode::STATUS_ERROR);
}

void FileSystemClass::_onResponse(const rpc::payload::FileReadResponse& msg) {
  if (_read_handler.is_valid()) {
    _read_handler(msg.content);
  }
}

#ifndef BRIDGE_TEST_NO_GLOBALS
FileSystemClass FileSystem;
#endif

#endif
