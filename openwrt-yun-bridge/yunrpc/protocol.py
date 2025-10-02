# yunrpc/protocol.py
"""
This file is part of Arduino Yun Ecosystem v2.

Copyright (C) 2025 Ignacio Santolin and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

This module defines the constants for the Yun v2 RPC protocol.
It is the Python counterpart to `rpc_protocol.h`.
"""
from enum import Enum, IntEnum
import struct

# Frame constants
START_BYTE = 0x7E

# < is for little-endian
# H is for unsigned short (2 bytes) -> PAYLOAD_LEN
# H is for unsigned short (2 bytes) -> COMMAND_ID
CRC_COVERED_HEADER_FORMAT = '<BHH'
CRC_COVERED_HEADER_SIZE = struct.calcsize(CRC_COVERED_HEADER_FORMAT)

# CRC format
# < is for little-endian
CRC_FORMAT = '<H'
CRC_SIZE = struct.calcsize(CRC_FORMAT)

# Minimum frame size is a frame with zero payload
MIN_FRAME_SIZE = CRC_COVERED_HEADER_SIZE + CRC_SIZE

# Frame types
class FrameType(Enum):
    COMMAND = 0x01
    RESPONSE = 0x02
    NOTIFICATION = 0x03 # Unsolicited message from MCU to Linux
    SYSTEM = 0x04

# Status codes for responses
class Status(Enum):
    OK = 0x00
    ERROR = 0x01
    CMD_UNKNOWN = 0x02
    MALFORMED = 0x03
    CRC_MISMATCH = 0x04
    TIMEOUT = 0x05
    NOT_IMPLEMENTED = 0x06

# Command identifiers
class Command(IntEnum):
    # Pin Operations
    CMD_SET_PIN_MODE = 0x10
    CMD_DIGITAL_WRITE = 0x11
    CMD_ANALOG_WRITE = 0x12
    CMD_DIGITAL_READ = 0x13
    CMD_ANALOG_READ = 0x14
    CMD_DIGITAL_READ_RESP = 0x15
    CMD_ANALOG_READ_RESP = 0x16

    # Console commands
    CMD_CONSOLE_WRITE = 0x20

    # DataStore commands
    CMD_DATASTORE_PUT = 0x30
    CMD_DATASTORE_GET = 0x31
    CMD_DATASTORE_GET_RESP = 0x81

    # Mailbox commands
    CMD_MAILBOX_READ = 0x40
    CMD_MAILBOX_AVAILABLE = 0x42
    CMD_MAILBOX_READ_RESP = 0x90
    CMD_MAILBOX_AVAILABLE_RESP = 0x92

    # FileIO commands
    CMD_FILE_WRITE = 0x50
    CMD_FILE_READ = 0x51
    CMD_FILE_REMOVE = 0x52
    CMD_FILE_READ_RESP = 0xA1

    # Process commands
    CMD_PROCESS_RUN = 0x60
    CMD_PROCESS_RUN_ASYNC = 0x61
    CMD_PROCESS_POLL = 0x62
    CMD_PROCESS_KILL = 0x63
    CMD_PROCESS_RUN_RESP = 0xB0
    CMD_PROCESS_RUN_ASYNC_RESP = 0xB1
    CMD_PROCESS_POLL_RESP = 0xB2

PROTOCOL_VERSION = 2
RPC_BUFFER_SIZE = 256

