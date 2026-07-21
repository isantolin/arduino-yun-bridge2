#include "services/FileSystem.h"

#include "Bridge.h"
#include "pb_encode.h"

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = 64U;

#define BRIDGE_FS_DEBUG(...)

template <typename Msg>
void copy_path(Msg& p, etl::string_view path) {
  const size_t len = etl::min(path.size(), sizeof(p.path) - 1U);
  pb_ostream_t stream = pb_ostream_from_buffer(
      reinterpret_cast<uint8_t*>(p.path), sizeof(p.path));
  (void)pb_write(&stream, reinterpret_cast<const uint8_t*>(path.data()), len);
  p.path[stream.bytes_written] = '\0';
}
}  // namespace

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  rpc::payload::FileWrite p = {};
  copy_path(p, path);
  const size_t d_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = static_cast<pb_size_t>(d_copy);
  if (d_copy > 0U) {
    pb_ostream_t stream =
        pb_ostream_from_buffer(p.data.bytes, sizeof(p.data.bytes));
    (void)pb_write(&stream, data.data(), d_copy);
  }
  (void)Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, p);
}

void FileSystemClass::read(
    etl::string_view path,
    typename FileSystemClass::FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead p = {};
  copy_path(p, path);
  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove p = {};
  copy_path(p, path);
  (void)Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0, p);
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) {
  auto res = bridge::hal::writeFile(
      etl::string_view(msg.path),
      etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
  (void)Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                             : rpc::StatusCode::STATUS_ERROR);
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  const etl::string_view path(msg.path);
  size_t offset = 0U;
  const uint32_t start_ms = millis();
  bool active = true;

  const uint16_t chunks[bridge::config::FILE_MAX_READ_CHUNKS] = {};
  etl::for_each(etl::begin(chunks), etl::end(chunks), [&](uint16_t) {
    if (!active || (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS))
      return;
    etl::array<uint8_t, kReadChunkSize> buffer;
    auto res = bridge::hal::readFileChunk(
        path, offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
    if (!res) {
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
      active = false;
      return;
    }
    rpc::payload::FileReadResponse p = {};
    p.content.size = static_cast<pb_size_t>(
        etl::min(res->bytes_read, sizeof(p.content.bytes)));
    pb_ostream_t stream =
        pb_ostream_from_buffer(p.content.bytes, sizeof(p.content.bytes));
    (void)pb_write(&stream, buffer.data(), p.content.size);
    (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p);

    if (!res->has_more) {
      rpc::payload::FileReadResponse empty_p = {};
      (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, empty_p);
      active = false;
      return;
    }
    offset += res->bytes_read;
  });
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  auto res = bridge::hal::removeFile(etl::string_view(msg.path));
  (void)Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                             : rpc::StatusCode::STATUS_ERROR);
}

void FileSystemClass::_onResponse(const rpc::payload::FileReadResponse& msg) {
  if (_read_handler.is_valid()) {
    _read_handler(
        etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

FileSystemType FileSystem;

#endif
