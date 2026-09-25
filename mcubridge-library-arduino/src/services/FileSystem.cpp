#include "services/FileSystem.h"

#if BRIDGE_ENABLE_FILESYSTEM

#include <etl/algorithm.h>
#include <etl/iterator.h>

namespace {
constexpr size_t kReadChunkSize = 64U;
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
  auto res = bridge::hal::writeFile(
      etl::string_view(msg.path),
      etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
  if (!res) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                      etl::string_view(rpc::status_reason::WRITE_FAILED));
  }
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  uint32_t start_ms = millis();
  bool finished = false;
  size_t offset = 0;
  etl::array<uint8_t, kReadChunkSize> buffer;

  uint8_t dummy = 0U;
  etl::fixed_iterator<uint8_t*> fixed_it(&dummy);

  etl::for_each_n(
      fixed_it, bridge::config::FILE_MAX_READ_CHUNKS, [&](uint8_t&) {
        if (finished) return;
        if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS) {
          finished = true;
          Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                            etl::string_view(rpc::status_reason::READ_FAILED));
          return;
        }

        auto res = bridge::hal::readFile(
            etl::string_view(msg.path), offset,
            etl::span<uint8_t>(buffer.data(), buffer.size()));
        if (!res) {
          Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                            etl::string_view(rpc::status_reason::READ_FAILED));
          finished = true;
          return;
        }

        rpc::payload::FileReadResponse p = {};
        rpc::Payload::copy_to_pb_bytes(
            p.content,
            etl::span<const uint8_t>(buffer.data(), res->bytes_read));
        (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p);

        if (!res->has_more) {
          finished = true;
          return;
        }
        offset += res->bytes_read;
      });
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  auto res = bridge::hal::removeFile(etl::string_view(msg.path));
  if (!res) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                      etl::string_view(rpc::status_reason::REMOVE_FAILED));
  }
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
