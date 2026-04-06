#include "FileSystem.h"
#include "Bridge.h"
#include "util/string_copy.h"

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = rpc::MAX_PAYLOAD_SIZE - 3U;

void send_read_response(etl::span<const uint8_t> data) {
  rpc::payload::FileReadResponse msg = {};
  msg.content = data;
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_READ_RESP, 0, msg);
}
}

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path, etl::span<const uint8_t> data) {
  rpc::payload::FileWrite msg = {};
  rpc::util::copy_string(path, msg.path, sizeof(msg.path));
  msg.data = data;
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_WRITE, 0, msg);
}

void FileSystemClass::read(etl::string_view path, FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc::payload::FileRead msg = {};
  rpc::util::copy_string(path, msg.path, sizeof(msg.path));
  if (!Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_READ, 0, msg)) {
    _read_handler.clear();
  }
}

[[maybe_unused]] void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove msg = {};
  rpc::util::copy_string(path, msg.path, sizeof(msg.path));
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_REMOVE, 0, msg);
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg) {
  // [SIL-2] Check hardware availability via HAL. Filesystem operations are 
  // only implemented if an SD card or external flash is present.
  if (bridge::hal::hasSD()) {
    auto res = bridge::hal::writeFile(msg.path, msg.data);
    if (res.has_value()) {
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
    } else {
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
    }
  } else { // GCOVR_EXCL_START — hasSD() is compile-time true on host
    // Graceful degradation: Report not implemented if hardware is missing.
    (void)Bridge.sendFrame(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
  } // GCOVR_EXCL_STOP
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  // [SIL-2] Graceful degradation: Read requires SD hardware support.
  if (!bridge::hal::hasSD()) { // GCOVR_EXCL_START — hasSD() is compile-time true on host
    (void)Bridge.sendFrame(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
    return;
  } // GCOVR_EXCL_STOP

  etl::array<uint8_t, kReadChunkSize> read_buffer;
  size_t offset = 0U;
  bool sent_payload = false;

  uint16_t chunk_count = 0U;
  uint32_t start_ms = bridge::now_ms();

  while (chunk_count++ < bridge::config::FILE_MAX_READ_CHUNKS && (bridge::now_ms() - start_ms < bridge::config::SERIAL_TIMEOUT_MS)) {
    auto res = bridge::hal::readFileChunk(
        msg.path,
        offset,
        etl::span<uint8_t>(read_buffer.data(), read_buffer.size()));

    if (!res.has_value()) {
      (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
      return;
    }

    const auto& result = res.value();
    if (result.bytes_read > 0U) {
      send_read_response(etl::span<const uint8_t>(read_buffer.data(), result.bytes_read));
      sent_payload = true;
      // [SIL-2] Guard against offset overflow on large files
      if (result.bytes_read > SIZE_MAX - offset) break; // GCOVR_EXCL_LINE — requires SIZE_MAX offset on host
      offset += result.bytes_read;
    } else if (!sent_payload) {
      send_read_response(etl::span<const uint8_t>());
      sent_payload = true;
    }

    if (!result.has_more) {
      break;
    }
  }

  if (sent_payload) {
    send_read_response(etl::span<const uint8_t>());
  }
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  if (!bridge::hal::hasSD()) { // GCOVR_EXCL_START — hasSD() is compile-time true on host
    (void)Bridge.sendFrame(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
    return;
  } // GCOVR_EXCL_STOP

  auto res = bridge::hal::removeFile(msg.path);
  if (res.has_value()) {
    (void)Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
  } else {
    (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::_onResponse(etl::span<const uint8_t> content) {
  if (_read_handler.is_valid()) {
    _read_handler(content);
  }
}
#endif
