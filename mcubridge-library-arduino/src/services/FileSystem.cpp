#include "services/FileSystem.h"

#include "Bridge.h"
#include "protocol/PacketBuilder.h"

#if BRIDGE_ENABLE_FILESYSTEM

FileSystemClass::FileSystemClass()
    : etl::imessage_router(
          rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP)) {}

void FileSystemClass::receive(const etl::imessage& msg) {
  if (msg.get_message_id() != rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP)) return;
  const auto& cmd_msg = static_cast<const bridge::router::CommandMessage&>(msg);
  Bridge._withPayload<rpc::payload::FileReadResponse>(
      cmd_msg, [this](const rpc::payload::FileReadResponse& pl) {
        if (_file_system_read_handler.is_valid()) {
          _file_system_read_handler(etl::span<const uint8_t>(pl.content, pl.length));
        }
      });
}

bool FileSystemClass::accepts(etl::message_id_t id) const {
  return id == rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
}

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  if (path.empty() || path.length() > rpc::RPC_MAX_FILEPATH_LENGTH) {
    return;
  }

  if (data.size() > rpc::MAX_PAYLOAD_SIZE - (path.length() + 1)) {
    return;
  }

  etl::vector<uint8_t, rpc::RPC_MAX_FILEPATH_LENGTH + 1> header;
  rpc::PacketBuilder(header).add_pascal_string(path);

  Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE,
                         etl::span<const uint8_t>(header.data(), header.size()),
                         data);
}

void FileSystemClass::read(etl::string_view path) {
  if (path.empty() || path.length() > rpc::RPC_MAX_FILEPATH_LENGTH) {
    return;
  }

  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_READ, path,
                                rpc::RPC_MAX_FILEPATH_LENGTH);
}

void FileSystemClass::remove(etl::string_view path) {
  if (path.empty() || path.length() > rpc::RPC_MAX_FILEPATH_LENGTH) {
    return;
  }

  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_REMOVE, path,
                                rpc::RPC_MAX_FILEPATH_LENGTH);
}

#endif
#if BRIDGE_ENABLE_FILESYSTEM
FileSystemClass FileSystem;
#endif
