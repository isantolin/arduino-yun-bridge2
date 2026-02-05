#include "Bridge.h"
#include "arduino/StringUtils.h"
#include <string.h>
#include "protocol/rpc_protocol.h"
#include "etl/algorithm.h"

void FileSystemClass::write(const char* filePath, const uint8_t* data,
                            size_t length) {
  if (!filePath || !data) return;
  const auto path_info = measure_bounded_cstring(
      filePath, rpc::RPC_MAX_FILEPATH_LENGTH);
  if (path_info.length == 0 || path_info.overflowed) return;
  const size_t path_len = path_info.length;

  // Header: [PathLen (1 byte)] [Path String]
  // We construct the header in a small stack buffer since paths are limited to 64 bytes.
  // MAX_PAYLOAD_SIZE is 64, so path must be smaller.
  uint8_t header[rpc::RPC_MAX_FILEPATH_LENGTH + 1];
  header[0] = static_cast<uint8_t>(path_len);
  
  // [SIL-2] Use etl::copy_n instead of memcpy
  etl::copy_n(filePath, path_len, header + 1);

  // Send potentially large data using chunking
  Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE, 
                         header, path_len + 1, 
                         data, length);
}

void FileSystemClass::remove(const char* filePath) {
  if (!filePath) return;
  const auto path_info = measure_bounded_cstring(
  filePath, rpc::RPC_MAX_FILEPATH_LENGTH);
  if (path_info.length == 0 || path_info.overflowed) return;
  const size_t path_len = path_info.length;

  // Use ETL vector as a safe buffer builder
  etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  payload.push_back(static_cast<uint8_t>(path_len));
  payload.insert(payload.end(), reinterpret_cast<const uint8_t*>(filePath), reinterpret_cast<const uint8_t*>(filePath) + path_len);

  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_FILE_REMOVE,
      payload.data(), static_cast<uint16_t>(payload.size()));
}

void FileSystemClass::read(const char* filePath) {
  if (!filePath || !*filePath) {
    return;
  }
  size_t len = strlen(filePath);
  if (len > rpc::RPC_MAX_FILEPATH_LENGTH) {
    return;
  }

  // Use ETL vector as a safe buffer builder
  etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  payload.push_back(static_cast<uint8_t>(len));
  payload.insert(payload.end(), reinterpret_cast<const uint8_t*>(filePath), reinterpret_cast<const uint8_t*>(filePath) + len);

  (void)Bridge.sendFrame(rpc::CommandId::CMD_FILE_READ, payload.data(), static_cast<uint16_t>(payload.size()));
}

void FileSystemClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  if (command == rpc::CommandId::CMD_FILE_READ_RESP) {
      if (_file_system_read_handler) {
        _file_system_read_handler(frame.payload.data(), frame.header.payload_length);
      }
  }
}
