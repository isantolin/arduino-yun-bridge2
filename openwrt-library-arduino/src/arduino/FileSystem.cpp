#include "Bridge.h"
#include "arduino/StringUtils.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

void FileSystemClass::write(const char* filePath, const uint8_t* data,
                            size_t length) {
  if (!filePath || !data) return;
  const auto path_info = measure_bounded_cstring(
      filePath, rpc::RPC_MAX_FILEPATH_LENGTH);
  if (path_info.length == 0 || path_info.overflowed) return;
  const size_t path_len = path_info.length;

  const size_t max_data = rpc::MAX_PAYLOAD_SIZE - 3 - path_len;
  if (length > max_data) {
    length = max_data;
  }

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  rpc::write_u16_be(payload + 1 + path_len, static_cast<uint16_t>(length));
  if (length > 0) {
    memcpy(payload + 3 + path_len, data, length);
  }

  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_FILE_WRITE,
      payload, static_cast<uint16_t>(path_len + length + 3));
}

void FileSystemClass::remove(const char* filePath) {
  if (!filePath) return;
  const auto path_info = measure_bounded_cstring(
  filePath, rpc::RPC_MAX_FILEPATH_LENGTH);
  if (path_info.length == 0 || path_info.overflowed) return;
  const size_t path_len = path_info.length;

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_FILE_REMOVE,
      payload, static_cast<uint16_t>(path_len + 1));
}

void FileSystemClass::read(const char* filePath) {
  if (!filePath || !*filePath) {
    return;
  }
  size_t len = strlen(filePath);
  if (len > rpc::RPC_MAX_FILEPATH_LENGTH) {
    return;
  }

  uint8_t* payload = Bridge.getScratchBuffer();
  payload[0] = static_cast<uint8_t>(len);
  memcpy(payload + 1, filePath, len);
  const uint16_t total = static_cast<uint16_t>(
      len + 1);
  (void)Bridge.sendFrame(rpc::CommandId::CMD_FILE_READ, payload, total);
}

void FileSystemClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  if (command == rpc::CommandId::CMD_FILE_READ_RESP) {
      if (_file_system_read_handler) {
        _file_system_read_handler(frame.payload.data(), frame.header.payload_length);
      }
  }
}
