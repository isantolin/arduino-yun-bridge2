#include "services/FileSystem.h"

#include "Bridge.h"
#include "protocol/pb_field_helpers.h"

#if BRIDGE_ENABLE_FILESYSTEM && defined(BRIDGE_HOST_TEST)
#include <cstdio>
#endif

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = 64U;

#if defined(BRIDGE_HOST_TEST)
#define BRIDGE_FS_DEBUG(...) fprintf(stderr, __VA_ARGS__)
#else
#define BRIDGE_FS_DEBUG(...) \
  do {                       \
  } while (false)
#endif

void send_read_response(etl::span<const uint8_t> content) {
  rpc::payload::FileReadResponse p;
  rpc::pb_field::copy_span_to_bytes_field(content, p.content);
  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p)) {
    Bridge.enterSafeState();
  }
}
}  // namespace

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  rpc::payload::FileWrite p;
  rpc::pb_field::copy_string_view_trunc(path, p.path);
  rpc::pb_field::copy_span_to_bytes_field(data, p.data);
  if (!Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, p)) {
    Bridge.enterSafeState();
  }
}

void FileSystemClass::read(etl::string_view path,
                           FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead p;
  rpc::pb_field::copy_string_view_trunc(path, p.path);

  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove p;
  rpc::pb_field::copy_string_view_trunc(path, p.path);
  if (!Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0, p)) {
    Bridge.enterSafeState();
  }
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) {
  auto res = bridge::hal::writeFile(
      etl::string_view(msg.path), rpc::pb_field::bytes_field_as_span(msg.data));
  if (!Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                            : rpc::StatusCode::STATUS_ERROR)) {
    Bridge.enterSafeState();
  }
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  BRIDGE_FS_DEBUG("[DEBUG] FS: Reading file: %s\\n", msg.path);
  size_t offset = 0;
  etl::array<uint8_t, kReadChunkSize> buffer;
  const uint32_t start_ms = millis();
  const etl::string_view path(msg.path);

  using bridge::etl_ext::CounterIterator;
  const auto stop = etl::find_if(
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
          if (!Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR)) {
            Bridge.enterSafeState();
          }
          return true;
        }
        BRIDGE_FS_DEBUG(
            "[DEBUG] FS: Sending chunk %u (%zu bytes, has_more=%d)\\n",
            static_cast<unsigned int>(chunk_idx), res->bytes_read, res->has_more);
        send_read_response(
            etl::span<const uint8_t>(buffer.data(), res->bytes_read));
        if (!res->has_more) {
          send_read_response(etl::span<const uint8_t>());
          return true;
        }
        offset += res->bytes_read;
        return false;
      });
  if (stop == CounterIterator(bridge::config::FILE_MAX_READ_CHUNKS)) {
    BRIDGE_FS_DEBUG("[DEBUG] FS: Read exhausted maximum chunk budget\\n");
  }
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  auto res = bridge::hal::removeFile(etl::string_view(msg.path));
  if (!Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK
                            : rpc::StatusCode::STATUS_ERROR)) {
    Bridge.enterSafeState();
  }
}

void FileSystemClass::_onResponse(const rpc::payload::FileReadResponse& msg) {
  if (_read_handler.is_valid()) {
    _read_handler(rpc::pb_field::bytes_field_as_span(msg.content));
  }
}

FileSystemClass FileSystem;

#endif
