"""This file is part of Arduino Yun Ecosystem v2.

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
import struct
from enum import IntEnum
from typing import Final

# --- Protocol Version ---
PROTOCOL_VERSION: Final[int] = 0x02

# --- Frame Structure ---
# The header format is: 1-byte version, 2-byte payload length, 2-byte command ID
# < denotes big-endian byte order.
CRC_COVERED_HEADER_FORMAT: str = ">BHH"
CRC_COVERED_HEADER_SIZE: int = struct.calcsize(CRC_COVERED_HEADER_FORMAT)

# The CRC format is: 2-byte unsigned short (big-endian)
CRC_FORMAT: str = ">H"
CRC_SIZE: int = struct.calcsize(CRC_FORMAT)

# A frame's minimum size is the header plus the CRC, for a zero-payload frame.
MIN_FRAME_SIZE: int = CRC_COVERED_HEADER_SIZE + CRC_SIZE
MAX_PAYLOAD_SIZE: int = 256
RPC_BUFFER_SIZE: int = 256


# --- DataStore Formats ---
# 1-byte key length (unsigned char, Big Endian)
DATASTORE_KEY_LEN_FORMAT: str = ">B"
DATASTORE_KEY_LEN_SIZE: int = struct.calcsize(DATASTORE_KEY_LEN_FORMAT)

# 1-byte value length (unsigned char, Big Endian)
DATASTORE_VALUE_LEN_FORMAT: str = ">B"
DATASTORE_VALUE_LEN_SIZE: int = struct.calcsize(DATASTORE_VALUE_LEN_FORMAT)


# --- Status Codes ---
# These are sent from the MCU to Linux to indicate the result of an operation.
class Status(IntEnum):
    OK = 0x00
    ERROR = 0x01
    CMD_UNKNOWN = 0x02
    MALFORMED = 0x03
    CRC_MISMATCH = 0x04
    TIMEOUT = 0x05
    NOT_IMPLEMENTED = 0x06
    ACK = 0x07  # Generic acknowledgment for fire-and-forget commands


# --- Command Identifiers ---
# Commands are sent from Linux to MCU, and responses are sent from MCU to Linux.
# By convention, response IDs are often related to the command ID.
class Command(IntEnum):
    # System Level
    CMD_GET_VERSION = 0x00
    CMD_GET_VERSION_RESP = 0x80
    CMD_GET_FREE_MEMORY = 0x01
    CMD_GET_FREE_MEMORY_RESP = 0x82

    # Flow Control
    CMD_XOFF = 0x08  # MCU -> Linux: Pause transmission
    CMD_XON = 0x09   # MCU -> Linux: Resume transmission


    # Pin Operations
    CMD_SET_PIN_MODE = 0x10
    CMD_DIGITAL_WRITE = 0x11
    CMD_ANALOG_WRITE = 0x12
    CMD_DIGITAL_READ = 0x13
    CMD_ANALOG_READ = 0x14
    CMD_DIGITAL_READ_RESP = 0x15
    CMD_ANALOG_READ_RESP = 0x16

    # Console I/O
    CMD_CONSOLE_WRITE = 0x20  # Can be Linux -> MCU or MCU -> Linux

    # DataStore (Key-Value Store)
    CMD_DATASTORE_PUT = 0x30
    CMD_DATASTORE_GET = 0x31
    CMD_DATASTORE_GET_RESP = 0x81

    # Mailbox
    CMD_MAILBOX_READ = 0x40
    CMD_MAILBOX_PROCESSED = 0x41 # MCU -> Linux: A message was processed
    CMD_MAILBOX_AVAILABLE = 0x42
    CMD_MAILBOX_READ_RESP = 0x90
    CMD_MAILBOX_AVAILABLE_RESP = 0x92

    # File I/O
    CMD_FILE_WRITE = 0x50
    CMD_FILE_READ = 0x51
    CMD_FILE_REMOVE = 0x52
    CMD_FILE_READ_RESP = 0xA1

    # Process Management
    CMD_PROCESS_RUN = 0x60
    CMD_PROCESS_RUN_ASYNC = 0x61
    CMD_PROCESS_POLL = 0x62
    CMD_PROCESS_KILL = 0x63
    CMD_PROCESS_RUN_RESP = 0xB0
    CMD_PROCESS_RUN_ASYNC_RESP = 0xB1
    CMD_PROCESS_POLL_RESP = 0xB2
