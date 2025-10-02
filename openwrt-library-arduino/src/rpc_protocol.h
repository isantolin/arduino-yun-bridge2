// rpc_protocol.h
#ifndef RPC_PROTOCOL_H
#define RPC_PROTOCOL_H

#include <stdint.h>

namespace rpc {

// Core Commands (Microcontroller <-> Linux)
enum Command : uint16_t {
    // Pin Operations
    CMD_SET_PIN_MODE = 0x10,
    CMD_DIGITAL_WRITE = 0x11,
    CMD_ANALOG_WRITE = 0x12,
    CMD_DIGITAL_READ = 0x13,
    CMD_ANALOG_READ = 0x14,
    CMD_DIGITAL_READ_RESP = 0x15,
    CMD_ANALOG_READ_RESP = 0x16,

    // Console commands
    CMD_CONSOLE_WRITE = 0x20,

    // DataStore commands
    CMD_DATASTORE_PUT = 0x30,
    CMD_DATASTORE_GET = 0x31,
    CMD_DATASTORE_GET_RESP = 0x81,

    // Mailbox commands
    CMD_MAILBOX_READ = 0x40,
    CMD_MAILBOX_AVAILABLE = 0x42,
    CMD_MAILBOX_READ_RESP = 0x90,
    CMD_MAILBOX_AVAILABLE_RESP = 0x92,

    // FileIO commands
    CMD_FILE_WRITE = 0x50,
    CMD_FILE_READ = 0x51,
    CMD_FILE_REMOVE = 0x52,
    CMD_FILE_READ_RESP = 0xA1,

    // Process commands
    CMD_PROCESS_RUN = 0x60,
    CMD_PROCESS_RUN_ASYNC = 0x61,
    CMD_PROCESS_POLL = 0x62,
    CMD_PROCESS_KILL = 0x63,
    CMD_PROCESS_RUN_RESP = 0xB0,
    CMD_PROCESS_RUN_ASYNC_RESP = 0xB1,
    CMD_PROCESS_POLL_RESP = 0xB2
};

} // namespace rpc

#endif // RPC_PROTOCOL_H
