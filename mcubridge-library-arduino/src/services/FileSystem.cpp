#include "services/FileSystem.h"

#include <etl/algorithm.h>
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
  bridge::utils::copy_to_buf(path, p.path);
  p.data.size = static_cast<pb_size_t>(
      bridge::utils::copy_bytes_to_buf(data, p.data.bytes));

  if (!Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                      etl::string_view(rpc::status_reason::WRITE_FAILED));
  }
}

void FileSystemClass::read(
    etl::string_view path,
    typename FileSystemClass::FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead p = {};
  bridge::utils::copy_to_buf(path, p.path);

  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove p = {};
  bridge::utils::copy_to_buf(path, p.path);

  if (!Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                      etl::string_view(rpc::status_reason::REMOVE_FAILED));
  }
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) {
  auto res = bridge::hal::writeFile(
      etl::string_view(msg.path),
      etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
  if (!Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                            : rpc::StatusCode::STATUS_ERROR)) {
  }
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  const etl::string_view path(msg.path);
  size_t offset = 0U;
  const uint32_t start_ms = millis();

  bool finished = false;
  etl::array<uint16_t, bridge::config::FILE_MAX_READ_CHUNKS> chunks;
  etl::iota(chunks.begin(), chunks.end(), 0U);

  etl::for_each(chunks.begin(), chunks.end(), [&](uint16_t chunk) {
    (void)chunk;
    if (finished) return;
    if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS) {
      finished = true;
      return;
    }
    etl::array<uint8_t, kReadChunkSize> buffer;
    auto res = bridge::hal::readFileChunk(
        path, offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
    if (!res) {
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
      finished = true;
      return;
    }

    rpc::payload::FileReadResponse p = {};
    p.content.size = static_cast<pb_size_t>(bridge::utils::copy_bytes_to_buf(
        etl::span<const uint8_t>(buffer.data(), res->bytes_read),
        p.content.bytes));
    (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p);

    if (!res->has_more) {
      rpc::payload::FileReadResponse empty_p = {};
      empty_p.content.size = 0U;
      (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, empty_p);
      finished = true;
      return;
    }

    offset += res->bytes_read;
  });
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  auto res = bridge::hal::removeFile(etl::string_view(msg.path));
  if (!Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                            : rpc::StatusCode::STATUS_ERROR)) {
  }
}

void FileSystemClass::_onResponse(const rpc::payload::FileReadResponse& msg) {
  if (_read_handler.is_valid()) {
    _read_handler(
        etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

FileSystemType FileSystem;

#endif
