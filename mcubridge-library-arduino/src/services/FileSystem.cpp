#include "FileSystem.h"
#include "Bridge.h"
#include "util/pb_copy.h"

#if BRIDGE_ENABLE_FILESYSTEM

namespace {
constexpr size_t kReadChunkSize = rpc::MAX_PAYLOAD_SIZE - 2U;

void send_read_response(etl::span<const uint8_t> data) {
  rpc::payload::FileReadResponse msg = {};
  rpc::util::pb_setup_encode_span(msg.content, data);
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_READ_RESP, msg);
}
}

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view path, etl::span<const uint8_t> data) {
  rpc::payload::FileWrite msg = {};
  rpc::util::pb_copy_string(path, msg.path, sizeof(msg.path));
  rpc::util::pb_setup_encode_span(msg.data, data);
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_WRITE, msg);
}

void FileSystemClass::read(etl::string_view path, FileSystemReadHandler handler) {
  rpc::payload::FileRead msg = {};
  rpc::util::pb_copy_string(path, msg.path, sizeof(msg.path));
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_READ, msg)) {
    _read_handler = handler;
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc::payload::FileRemove msg = {};
  rpc::util::pb_copy_string(path, msg.path, sizeof(msg.path));
  Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_REMOVE, msg);
}

void FileSystemClass::_onWrite(const rpc::payload::FileWrite& msg, etl::span<const uint8_t> data) {
  // [SIL-2] Check hardware availability via HAL. Filesystem operations are 
  // only implemented if an SD card or external flash is present.
  if (bridge::hal::hasSD()) {
    if (bridge::hal::writeFile(msg.path, data)) {
      Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
    } else {
      Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
    }
  } else {
    // Graceful degradation: Report not implemented if hardware is missing.
    Bridge.sendFrame(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
  }
}

void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  // [SIL-2] Graceful degradation: Read requires SD hardware support.
  if (!bridge::hal::hasSD()) {
    Bridge.sendFrame(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
    return;
  }

  etl::array<uint8_t, kReadChunkSize> read_buffer;
  size_t offset = 0U;
  bool sent_payload = false;

  while (true) {
    size_t bytes_read = 0U;
    bool has_more = false;
    const bool read_ok = bridge::hal::readFileChunk(
        msg.path,
        offset,
        etl::span<uint8_t>(read_buffer.data(), read_buffer.size()),
        bytes_read,
        has_more);
    if (!read_ok) {
      Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
      return;
    }

    if (bytes_read > 0U) {
      send_read_response(etl::span<const uint8_t>(read_buffer, bytes_read));
      sent_payload = true;
      offset += bytes_read;
    } else if (!sent_payload) {
      send_read_response(etl::span<const uint8_t>());
    }

    if (!has_more) {
      break;
    }
  }

  if (sent_payload) {
    send_read_response(etl::span<const uint8_t>());
  }
}

void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  if (!bridge::hal::hasSD()) {
    Bridge.sendFrame(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
    return;
  }

  if (bridge::hal::removeFile(msg.path)) {
    Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
  } else {
    Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::_onResponse(etl::span<const uint8_t> content) {
  if (_read_handler.is_valid()) {
    _read_handler(content);
  }
}
#endif
