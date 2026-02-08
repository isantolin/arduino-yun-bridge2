#include "Bridge.h"
#include <string.h>
#include "protocol/rpc_protocol.h"
#include "etl/algorithm.h"

void FileSystemClass::write(const char* filePath, const uint8_t* data,
                            size_t length) {
  if (!filePath || !data) return;
  etl::string_view path(filePath);
  if (path.empty() || path.length() > rpc::RPC_MAX_FILEPATH_LENGTH) return;

  uint8_t header[rpc::RPC_MAX_FILEPATH_LENGTH + 1];
  header[0] = static_cast<uint8_t>(path.length());
  etl::copy_n(path.data(), path.length(), header + 1);

  Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE, 
                         header, path.length() + 1, 
                         data, length);
}

void FileSystemClass::remove(const char* filePath) {
  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_REMOVE, 
                                filePath, rpc::RPC_MAX_FILEPATH_LENGTH);
}

void FileSystemClass::read(const char* filePath) {
  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_READ, 
                                filePath, rpc::RPC_MAX_FILEPATH_LENGTH);
}

void FileSystemClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  if (command == rpc::CommandId::CMD_FILE_READ_RESP) {
      if (_file_system_read_handler) {
        _file_system_read_handler(frame.payload.data(), frame.header.payload_length);
      }
  }
}
