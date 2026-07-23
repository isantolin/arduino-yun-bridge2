#include "services/FileSystem.h"

#include <etl/algorithm.h>
#include <etl/numeric.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = 64U;

#define BRIDGE_FS_DEBUG(...)
}  // namespace

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  rpc::payload::FileWrite p = {};
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) {
    etl::copy_n(path.begin(), p_copy, p.path);
  }

  const size_t d_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)d_copy;
  if (d_copy > 0U) {
    etl::copy_n(data.data(), d_copy, p.data.bytes);
  }
  if (!Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, p)) {
  }
}

void FileSystemClass::read(
    etl::string_view path,
    typename FileSystemClass::FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead p = {};
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) {
    etl::copy_n(path.begin(), p_copy, p.path);
  }

  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove p = {};
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) {
    etl::copy_n(path.begin(), p_copy, p.path);
  }
  if (!Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0, p)) {
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
  BRIDGE_FS_DEBUG("[DEBUG] FS: Reading file: %s\n", msg.path);
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
      BRIDGE_FS_DEBUG("[DEBUG] FS: Read TIMEOUT at offset %zu\n", offset);
      finished = true;
      return;
    }
    etl::array<uint8_t, kReadChunkSize> buffer;
    auto res = bridge::hal::readFileChunk(
        path, offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
    if (!res) {
      BRIDGE_FS_DEBUG("[DEBUG] FS: Read FAILED at offset %zu\n", offset);
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
      finished = true;
      return;
    }
    BRIDGE_FS_DEBUG("[DEBUG] FS: Sending chunk (%zu bytes, has_more=%d)\n",
                    res->bytes_read, res->has_more);

    rpc::payload::FileReadResponse p = {};
    const size_t to_copy = etl::min(res->bytes_read, sizeof(p.content.bytes));
    p.content.size = static_cast<pb_size_t>(to_copy);
    if (to_copy > 0U) {
      etl::copy_n(buffer.data(), to_copy, p.content.bytes);
    }
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
