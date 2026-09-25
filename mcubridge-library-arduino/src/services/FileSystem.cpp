#include "services/FileSystem.h"

#if BRIDGE_ENABLE_FILESYSTEM

#include <etl/algorithm.h>
#include <etl/utility.h>

namespace {
constexpr size_t kReadChunkSize = 64U;

template <auto HalFn, typename... Args>
inline void _executeHalAction(const char* reason, Args&&... args) {
  if (!HalFn(etl::forward<Args>(args)...)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                      etl::string_view(reason));
  }
}

bool _readAndSendChunk(etl::string_view path, size_t& offset,
                       etl::span<uint8_t> buffer) {
  auto res = bridge::hal::readFile(path, offset, buffer);
  if (!res) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                      etl::string_view(rpc::status_reason::READ_FAILED));
    return false;
  }

  rpc::payload::FileReadResponse p = {};
  rpc::Payload::copy_to_pb_bytes(
      p.content,
      etl::span<const uint8_t>(buffer.data(), res->bytes_read));
  (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p);

  offset += res->bytes_read;
  return res->has_more;
}
}  // namespace

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  rpc::payload::FileWrite p = {};
  rpc::Payload::copy_to_pb_string(p.path, path);
  rpc::Payload::copy_to_pb_bytes(p.data, data);

  Bridge.sendOrEmitStatus(rpc::CommandId::CMD_FILE_WRITE, 0, p,
                          etl::string_view(rpc::status_reason::WRITE_FAILED));
}

void FileSystemClass::read(
    etl::string_view path,
    typename FileSystemClass::FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead p = {};
  rpc::Payload::copy_to_pb_string(p.path, path);

  Bridge.sendOrEmitStatus(rpc::CommandId::CMD_FILE_READ, 0, p);
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove p = {};
  rpc::Payload::copy_to_pb_string(p.path, path);

  Bridge.sendOrEmitStatus(rpc::CommandId::CMD_FILE_REMOVE, 0, p,
                          etl::string_view(rpc::status_reason::REMOVE_FAILED));
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) {
  _executeHalAction<bridge::hal::writeFile>(
      rpc::status_reason::WRITE_FAILED, etl::string_view(msg.path),
      etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  etl::array<uint8_t, kReadChunkSize> buffer;
  size_t offset = 0;
  bool more = true;
  auto step = [&]() {
    if (more) {
      more = _readAndSendChunk(
          etl::string_view(msg.path), offset,
          etl::span<uint8_t>(buffer.data(), buffer.size()));
    }
  };
  step();
  step();
  step();
  step();
  step();
  step();
  step();
  step();
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  _executeHalAction<bridge::hal::removeFile>(
      rpc::status_reason::REMOVE_FAILED, etl::string_view(msg.path));
}

void FileSystemClass::_onResponse(const rpc::payload::FileReadResponse& msg) {
  if (_read_handler.is_valid()) {
    auto handler = _read_handler;
    if (msg.content.size == 0U) {
      _read_handler = FileSystemReadHandler{};
    }
    handler(etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

#endif  // BRIDGE_ENABLE_FILESYSTEM
