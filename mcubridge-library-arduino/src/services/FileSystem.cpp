#include "services/FileSystem.h"

#include <etl/algorithm.h>
#include <etl/fixed_iterator.h>
#include <etl/numeric.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

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
  _pending_reads.clear();
  _pending_reads.push(handler);
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
  const bool sent = Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                                         : rpc::StatusCode::STATUS_ERROR);
  (void)sent;
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  const etl::string_view path(msg.path);
  size_t offset = 0U;
  const uint32_t start_ms = millis();

  bool finished = false;
  uint8_t dummy = 0U;
  etl::fixed_iterator<uint8_t*> fixed_it(&dummy);

  etl::for_each_n(
      fixed_it, bridge::config::FILE_MAX_READ_CHUNKS, [&](uint8_t&) {
        if (finished) return;
        if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS) {
          finished = true;
          return;
        }
        etl::array<uint8_t, kReadChunkSize> buffer;
        auto res = bridge::hal::readFileChunk(
            path, offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
        if (!res) {
          const bool sent = Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
          (void)sent;
          finished = true;
          return;
        }

        rpc::payload::FileReadResponse p = {};
        rpc::Payload::copy_to_pb_bytes(
            p.content,
            etl::span<const uint8_t>(buffer.data(), res->bytes_read));
        if (!Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p)) {
          finished = true;
          return;
        }

        if (!res->has_more) {
          rpc::payload::FileReadResponse empty_p = {};
          const bool sent =
              Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, empty_p);
          (void)sent;
          finished = true;
          return;
        }

        offset += res->bytes_read;
      });
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  auto res = bridge::hal::removeFile(etl::string_view(msg.path));
  const bool sent = Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                                         : rpc::StatusCode::STATUS_ERROR);
  (void)sent;
}

void FileSystemClass::_onResponse(const rpc::payload::FileReadResponse& msg) {
  if (_pending_reads.empty()) return;
  const FileSystemReadHandler handler = _pending_reads.front();
  if (msg.content.size == 0U) {
    _pending_reads.pop();
  }
  if (handler.is_valid()) {
    handler(etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

FileSystemType FileSystem;

#endif
