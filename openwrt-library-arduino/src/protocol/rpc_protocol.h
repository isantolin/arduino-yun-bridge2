/*
 * This file is part of Arduino Yun Ecosystem v2.

 * Copyright (C) 2025 Ignacio Santolin and contributors

 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.

 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */
#ifndef RPC_PROTOCOL_H
#define RPC_PROTOCOL_H

#include "rpc_frame.h"

static_assert(
    rpc::PROTOCOL_VERSION == 0x02,
    "Protocol version mismatch with spec.toml"
);
static_assert(
    rpc::MAX_PAYLOAD_SIZE == 256,
    "Max payload size mismatch with spec.toml"
);
static_assert(
    rpc::CRC_TRAILER_SIZE == 4,
    "CRC trailer size mismatch with spec.toml"
);

constexpr unsigned int RPC_BUFFER_SIZE = 256;
constexpr std::size_t RPC_HANDSHAKE_NONCE_LENGTH = 16u;
constexpr std::size_t RPC_HANDSHAKE_TAG_LENGTH = 16u;
constexpr const char RPC_HANDSHAKE_TAG_ALGORITHM[] = "HMAC-SHA256";
constexpr const char RPC_HANDSHAKE_TAG_DESCRIPTION[] = "HMAC-SHA256(secret, nonce) truncated to 16 bytes";
constexpr const char RPC_HANDSHAKE_CONFIG_FORMAT[] = ">HBI";
constexpr const char RPC_HANDSHAKE_CONFIG_DESCRIPTION[] = "Serialized timing config: ack_timeout_ms (uint16), ack_retry_limit (uint8), response_timeout_ms (uint32)";
constexpr std::size_t RPC_HANDSHAKE_CONFIG_SIZE = 7u;
constexpr unsigned int RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS = 25;
constexpr unsigned int RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS = 60000;
constexpr unsigned int RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS = 100;
constexpr unsigned int RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS = 180000;
constexpr unsigned int RPC_HANDSHAKE_RETRY_LIMIT_MIN = 1;
constexpr unsigned int RPC_HANDSHAKE_RETRY_LIMIT_MAX = 8;

// Status Codes
#define STATUS_OK 0x00
#define STATUS_ERROR 0x01
#define STATUS_CMD_UNKNOWN 0x02
#define STATUS_MALFORMED 0x03
#define STATUS_CRC_MISMATCH 0x04
#define STATUS_TIMEOUT 0x05
#define STATUS_NOT_IMPLEMENTED 0x06
#define STATUS_ACK 0x07

// Command Identifiers
// System
#define CMD_GET_VERSION 0x00
#define CMD_GET_VERSION_RESP 0x80
#define CMD_GET_FREE_MEMORY 0x01
#define CMD_GET_FREE_MEMORY_RESP 0x82
#define CMD_LINK_SYNC 0x02
#define CMD_LINK_SYNC_RESP 0x83
#define CMD_LINK_RESET 0x03
#define CMD_LINK_RESET_RESP 0x84

// Flow Control
#define CMD_XOFF 0x08
#define CMD_XON 0x09

// Gpio
#define CMD_SET_PIN_MODE 0x10
#define CMD_DIGITAL_WRITE 0x11
#define CMD_ANALOG_WRITE 0x12
#define CMD_DIGITAL_READ 0x13
#define CMD_ANALOG_READ 0x14
#define CMD_DIGITAL_READ_RESP 0x15
#define CMD_ANALOG_READ_RESP 0x16

// Console
#define CMD_CONSOLE_WRITE 0x20

// Datastore
#define CMD_DATASTORE_PUT 0x30
#define CMD_DATASTORE_GET 0x31
#define CMD_DATASTORE_GET_RESP 0x81

// Mailbox
#define CMD_MAILBOX_READ 0x40
#define CMD_MAILBOX_PROCESSED 0x41
#define CMD_MAILBOX_AVAILABLE 0x42
#define CMD_MAILBOX_PUSH 0x43
#define CMD_MAILBOX_READ_RESP 0x90
#define CMD_MAILBOX_AVAILABLE_RESP 0x92

// Filesystem
#define CMD_FILE_WRITE 0x50
#define CMD_FILE_READ 0x51
#define CMD_FILE_REMOVE 0x52
#define CMD_FILE_READ_RESP 0xA1

// Process
#define CMD_PROCESS_RUN 0x60
#define CMD_PROCESS_RUN_ASYNC 0x61
#define CMD_PROCESS_POLL 0x62
#define CMD_PROCESS_KILL 0x63
#define CMD_PROCESS_RUN_RESP 0xB0
#define CMD_PROCESS_RUN_ASYNC_RESP 0xB1
#define CMD_PROCESS_POLL_RESP 0xB2

#endif  // RPC_PROTOCOL_H
