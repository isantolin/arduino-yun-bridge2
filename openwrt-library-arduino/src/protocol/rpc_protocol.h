/*
 * This file is part of Arduino Yun Ecosystem v2.
 *
 * Copyright (C) 2025 Ignacio Santolin and contributors
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */
#ifndef RPC_PROTOCOL_H
#define RPC_PROTOCOL_H

#include "rpc_frame.h"

// Protocol constants (PROTOCOL_VERSION, MAX_PAYLOAD_SIZE) are defined in
// rpc_frame.h

// Status Codes
#define STATUS_OK 0x00
#define STATUS_ERROR 0x01
#define STATUS_CMD_UNKNOWN 0x02
// The following status codes are defined for future use but are not yet sent by the MCU.
#define STATUS_MALFORMED 0x03
#define STATUS_CRC_MISMATCH 0x04
#define STATUS_TIMEOUT 0x05
#define STATUS_NOT_IMPLEMENTED 0x06
#define STATUS_ACK 0x07

// System Commands
#define CMD_GET_VERSION 0x00
#define CMD_GET_VERSION_RESP 0x80
#define CMD_GET_FREE_MEMORY 0x01
#define CMD_GET_FREE_MEMORY_RESP 0x82

// Flow Control & System Commands
#define CMD_XOFF 0x08
#define CMD_XON 0x09


// Command Identifiers
#define CMD_SET_PIN_MODE 0x10
#define CMD_DIGITAL_WRITE 0x11
#define CMD_ANALOG_WRITE 0x12
#define CMD_DIGITAL_READ 0x13
#define CMD_ANALOG_READ 0x14
#define CMD_DIGITAL_READ_RESP 0x15
#define CMD_ANALOG_READ_RESP 0x16

#define CMD_CONSOLE_WRITE 0x20

#define CMD_DATASTORE_PUT 0x30
#define CMD_DATASTORE_GET 0x31
#define CMD_DATASTORE_GET_RESP 0x81

#define CMD_MAILBOX_READ 0x40
#define CMD_MAILBOX_PROCESSED 0x41
#define CMD_MAILBOX_AVAILABLE 0x42
#define CMD_MAILBOX_PUSH 0x43
#define CMD_MAILBOX_READ_RESP 0x90
#define CMD_MAILBOX_AVAILABLE_RESP 0x92

#define CMD_FILE_WRITE 0x50
#define CMD_FILE_READ 0x51
#define CMD_FILE_REMOVE 0x52
#define CMD_FILE_READ_RESP 0xA1

#define CMD_PROCESS_RUN 0x60
#define CMD_PROCESS_RUN_ASYNC 0x61
#define CMD_PROCESS_POLL 0x62
#define CMD_PROCESS_KILL 0x63
#define CMD_PROCESS_RUN_RESP 0xB0
#define CMD_PROCESS_RUN_ASYNC_RESP 0xB1
#define CMD_PROCESS_POLL_RESP 0xB2

#endif  // RPC_PROTOCOL_H