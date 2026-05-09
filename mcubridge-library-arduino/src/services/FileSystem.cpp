#include "services/FileSystem.h"
#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = rpc::MAX_PAYLOAD_SIZE - 3U;

void send_read_response(etl::span<const uint8_t> content) {
  (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0,
                    rpc::payload::FileReadResponse{content});
}
}  // namespace

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  (void)Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0,
                    rpc::payload::FileWrite{path, data});
}

void FileSystemClass::read(etl::string_view path,
                           FileSystemReadHandler handler) {
  _read_handler = handler;
  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0,
                   rpc::payload::FileRead{path})) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::remove(etl::string_view path) {
  (void)Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0,
                    rpc::payload::FileRemove{path});
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) {
  auto res = bridge::hal::writeFile(
      etl::string_view(msg.path.data(), msg.path.size()), msg.data);
  (void)Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                             : rpc::StatusCode::STATUS_ERROR);
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  size_t offset = 0;
  // [SIL-2] Reverting to local stack buffer to prevent memory collision.
  // Bridge.borrowTransientBuffer() cannot be used here because send_read_response
  // calls Bridge.send, which uses the same buffer for MsgPack encoding.
  etl::array<uint8_t, kReadChunkSize> buffer;
  const uint32_t start_ms = millis();
  const etl::string_view path(msg.path.data(), msg.path.size());

  // [SIL-2] Use CounterIterator to avoid large stack-allocated dummy arrays.
  using bridge::utils::CounterIterator;
  (void)etl::find_if(
      CounterIterator(0), CounterIterator(bridge::config::FILE_MAX_READ_CHUNKS),
      [&](uint32_t) {
        if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS)
          return true;

        auto res = bridge::hal::readFileChunk(
            path, offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
        if (!res) {
          (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
          return true;
        }
        send_read_response(
            etl::span<const uint8_t>(buffer.data(), res->bytes_read));
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
