#include "services/FileSystem.h"

#include "Bridge.h"
#include "etl/vector.h"
#include "protocol/PacketBuilder.h"

#if BRIDGE_ENABLE_FILESYSTEM

FileSystemClass::FileSystemClass() = default;

void FileSystemClass::write(etl::string_view filePath,
                            etl::span<const uint8_t> data) {
  if (filePath.empty() || data.empty()) return;

  if (filePath.length() > rpc::RPC_MAX_FILEPATH_LENGTH - 1) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  constexpr size_t HEADER_METADATA_SIZE = 3;
  etl::vector<uint8_t, rpc::RPC_MAX_FILEPATH_LENGTH + HEADER_METADATA_SIZE> header;
  rpc::PacketBuilder builder(header);
  builder.add_pascal_string(filePath);
  builder.add_value(static_cast<uint16_t>(data.size()));

  Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE,
                         etl::span<const uint8_t>(header.data(), header.size()),
                         data);
}

void FileSystemClass::remove(etl::string_view filePath) {
  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_REMOVE, filePath,
                                rpc::RPC_MAX_FILEPATH_LENGTH - 1);
}

void FileSystemClass::read(etl::string_view filePath) {
  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_READ, filePath,
                                rpc::RPC_MAX_FILEPATH_LENGTH - 1);
}

#endif
