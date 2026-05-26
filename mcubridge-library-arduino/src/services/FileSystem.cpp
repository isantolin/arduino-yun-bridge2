#include "services/FileSystem.h"

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM && defined(BRIDGE_HOST_TEST)
#include <cstdio>
#endif

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = 64U;

#if defined(BRIDGE_HOST_TEST)
#define BRIDGE_FS_DEBUG(...) fprintf(stderr, __VA_ARGS__)
#else
#define BRIDGE_FS_DEBUG(...) ((void)0)
#endif

void send_read_response(etl::span<const uint8_t> content) {
  rpc::payload::FileReadResponse p;
  rpc::payload::copy_to_pb_bytes(p.content, content.data(),
                                 content.size());
  (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p);
}
}  // namespace

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  rpc::payload::FileWrite p;
  rpc::payload::copy_to_pb_string(p.path, path);
  rpc::payload::copy_to_pb_bytes(p.data, data.data(), data.size());
  (void)Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, p);
}

void FileSystemClass::read(etl::string_view path,
                           FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead p;
  rpc::payload::copy_to_pb_string(p.path, path);
  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

[[maybe_unused]] void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove p;
  rpc::payload::copy_to_pb_string(p.path, path);
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
  BRIDGE_FS_DEBUG("[DEBUG] FS: Reading file: %s\\n", msg.path);
  size_t offset = 0;
  etl::array<uint8_t, kReadChunkSize> buffer;
  const uint32_t start_ms = millis();
  const etl::string_view path(msg.path);

  using bridge::etl_ext::CounterIterator;
  (void)etl::find_if(
      CounterIterator<uint16_t>(0U),
      CounterIterator(bridge::config::FILE_MAX_READ_CHUNKS),
      [&](uint32_t chunk_idx) {
        if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS) {
          BRIDGE_FS_DEBUG("[DEBUG] FS: Read TIMEOUT at offset %zu\\n", offset);
          return true;
        }

        auto res = bridge::hal::readFileChunk(
            path, offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
        if (!res) {
          BRIDGE_FS_DEBUG("[DEBUG] FS: Read FAILED at offset %zu\\n", offset);
          (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
          return true;
        }
        BRIDGE_FS_DEBUG(
            "[DEBUG] FS: Sending chunk %u (%zu bytes, has_more=%d)\\n",
            (unsigned int)chunk_idx, res->bytes_read, res->has_more);
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
  auto res = bridge::hal::removeFile(etl::string_view(msg.path));
  (void)Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                             : rpc::StatusCode::STATUS_ERROR);
}

void FileSystemClass::_onResponse(const rpc::payload::FileReadResponse& msg) {
  if (_read_handler.is_valid()) {
    _read_handler(etl::span<const uint8_t>(msg.content.bytes,
                                           msg.content.size));
  }
}

FileSystemClass FileSystem;

#endif
