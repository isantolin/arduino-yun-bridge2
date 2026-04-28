#include "services/FileSystem.h"
#include "Bridge.h"

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
    Bridge.emitStatus<rpc::StatusCode::STATUS_ERROR>();
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
  etl::array<uint8_t, kReadChunkSize> buffer;
  uint32_t start_ms = millis();
  etl::string_view path(msg.path.data(), msg.path.size());

  etl::array<uint16_t, bridge::config::FILE_MAX_READ_CHUNKS> chunks;
  (void)etl::find_if(chunks.begin(), chunks.end(), [&](uint16_t) {
    if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS) return true;

    auto res = bridge::hal::readFileChunk(path, offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
    if (!res) {
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
      return true;
    }
    send_read_response(etl::span<const uint8_t>(buffer.data(), res->bytes_read));
    if (!res->has_more) {
      send_read_response(etl::span<const uint8_t>());
      return true;
    }
    offset += res->bytes_read;
    return false;
  });
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

FileSystemClass FileSystem;

#endif
