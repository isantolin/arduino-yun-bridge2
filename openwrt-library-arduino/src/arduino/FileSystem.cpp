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
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case rpc::CommandId::CMD_FILE_READ_RESP:
      if (_file_system_read_handler && payload_length >= 2 && payload_data) {
        uint16_t data_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + data_len);
        if (payload_length >= expected) {
          _file_system_read_handler(payload_data + 2, data_len);
        }
      }
      break;
    case rpc::CommandId::CMD_FILE_WRITE:
      if (payload_length > 1 && payload_data) {
           uint8_t path_len = payload_data[0];
           if (path_len < payload_length) {
               const char* path_start = reinterpret_cast<const char*>(payload_data + 1);
               const uint8_t* data_ptr = payload_data + 1 + path_len;
               size_t data_len = payload_length - 1 - path_len;

               bool is_eeprom = false;
#if defined(ARDUINO_ARCH_AVR)
               const size_t prefix_len = 8; // "/eeprom/" length
               if (path_len >= prefix_len) {
                   if (strncmp_P(path_start, PSTR("/eeprom/"), prefix_len) == 0) {
                       is_eeprom = true;
                   }
               }
#else
               const char prefix[] = "/eeprom/";
               const size_t prefix_len = sizeof(prefix) - 1;
               if (path_len >= prefix_len) {
                   if (strncmp(path_start, prefix, prefix_len) == 0) {
                       is_eeprom = true;
                   }
               }
#endif

#if defined(ARDUINO_ARCH_AVR)
               if (is_eeprom && data_len > 0) {
                   int offset = 0;
                   if (path_len > prefix_len) {
                       const char* num_start = path_start + prefix_len;
                       size_t num_len = path_len - prefix_len;
                       bool valid_num = true;
                       for (size_t i = 0; i < num_len; ++i) {
                           if (num_start[i] < '0' || num_start[i] > '9') {
                               valid_num = false;
                               break;
                           }
                       }
                       
                       if (valid_num) {
                           for (size_t i = 0; i < num_len; ++i) {
                               char c = num_start[i];
                               offset = offset * 10 + (c - '0');
                           }
                           
                           for (size_t i = 0; i < data_len; i++) {
                               eeprom_update_byte((uint8_t*)(offset + i), data_ptr[i]);
                           }
                       }
                   }
               }
#else
               (void)data_ptr;
               (void)data_len;
               (void)is_eeprom;
#endif
           }
      }
      break;
    default:
      break;
  }
}

void FileSystemClass::onFileSystemReadResponse(FileSystemReadHandler handler) {
  _file_system_read_handler = handler;
}
