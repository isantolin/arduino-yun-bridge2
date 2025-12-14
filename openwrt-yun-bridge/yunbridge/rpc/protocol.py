"""Auto-generated protocol bindings. Do not edit manually."""
from __future__ import annotations
import struct
from enum import IntEnum
from typing import Final

PROTOCOL_VERSION: Final[int] = 2
DEFAULT_BAUDRATE: Final[int] = 115200
MAX_PAYLOAD_SIZE: Final[int] = 128
RPC_BUFFER_SIZE: Final[int] = 128

HANDSHAKE_NONCE_LENGTH: Final[int] = 16
HANDSHAKE_TAG_LENGTH: Final[int] = 16
HANDSHAKE_TAG_ALGORITHM: Final[str] = "HMAC-SHA256"
HANDSHAKE_TAG_DESCRIPTION: Final[str] = "HMAC-SHA256(secret, nonce) truncated to 16 bytes"
HANDSHAKE_CONFIG_FORMAT: Final[str] = ">HBI"
HANDSHAKE_CONFIG_DESCRIPTION: Final[str] = "Serialized timing config: ack_timeout_ms (uint16), ack_retry_limit (uint8), response_timeout_ms (uint32)"
HANDSHAKE_CONFIG_SIZE: Final[int] = struct.calcsize(HANDSHAKE_CONFIG_FORMAT)
HANDSHAKE_ACK_TIMEOUT_MIN_MS: Final[int] = 25
HANDSHAKE_ACK_TIMEOUT_MAX_MS: Final[int] = 60000
HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS: Final[int] = 100
HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS: Final[int] = 180000
HANDSHAKE_RETRY_LIMIT_MIN: Final[int] = 1
HANDSHAKE_RETRY_LIMIT_MAX: Final[int] = 8

DATASTORE_KEY_LEN_FORMAT: Final[str] = ">B"
DATASTORE_KEY_LEN_SIZE: Final[int] = struct.calcsize(DATASTORE_KEY_LEN_FORMAT)
DATASTORE_VALUE_LEN_FORMAT: Final[str] = ">B"
DATASTORE_VALUE_LEN_SIZE: Final[int] = struct.calcsize(DATASTORE_VALUE_LEN_FORMAT)
CRC_COVERED_HEADER_FORMAT: Final[str] = ">BHH"
CRC_COVERED_HEADER_SIZE: Final[int] = struct.calcsize(CRC_COVERED_HEADER_FORMAT)
CRC_FORMAT: Final[str] = ">I"
CRC_SIZE: Final[int] = struct.calcsize(CRC_FORMAT)
MIN_FRAME_SIZE: Final[int] = CRC_COVERED_HEADER_SIZE + CRC_SIZE


class Status(IntEnum):
    OK = 0  # Operation completed successfully.
    ERROR = 1  # Generic failure.
    CMD_UNKNOWN = 2  # Command not recognized.
    MALFORMED = 3  # Payload had invalid structure.
    OVERFLOW = 8  # Frame exceeded buffer size.
    CRC_MISMATCH = 4  # CRC check failed.
    TIMEOUT = 5  # Operation timed out.
    NOT_IMPLEMENTED = 6  # Command defined but not supported.
    ACK = 7  # Generic acknowledgement for fire-and-forget commands.


class Command(IntEnum):
    CMD_GET_VERSION = 0
    CMD_GET_VERSION_RESP = 128
    CMD_GET_FREE_MEMORY = 1
    CMD_GET_FREE_MEMORY_RESP = 130
    CMD_GET_TX_DEBUG_SNAPSHOT = 4
    CMD_GET_TX_DEBUG_SNAPSHOT_RESP = 133
    CMD_LINK_SYNC = 2
    CMD_LINK_SYNC_RESP = 131
    CMD_LINK_RESET = 3
    CMD_LINK_RESET_RESP = 132
    CMD_XOFF = 8
    CMD_XON = 9
    CMD_SET_PIN_MODE = 16
    CMD_DIGITAL_WRITE = 17
    CMD_ANALOG_WRITE = 18
    CMD_DIGITAL_READ = 19
    CMD_ANALOG_READ = 20
    CMD_DIGITAL_READ_RESP = 21
    CMD_ANALOG_READ_RESP = 22
    CMD_CONSOLE_WRITE = 32
    CMD_DATASTORE_PUT = 48
    CMD_DATASTORE_GET = 49
    CMD_DATASTORE_GET_RESP = 129
    CMD_MAILBOX_READ = 64
    CMD_MAILBOX_PROCESSED = 65
    CMD_MAILBOX_AVAILABLE = 66
    CMD_MAILBOX_PUSH = 67
    CMD_MAILBOX_READ_RESP = 144
    CMD_MAILBOX_AVAILABLE_RESP = 146
    CMD_FILE_WRITE = 80
    CMD_FILE_READ = 81
    CMD_FILE_REMOVE = 82
    CMD_FILE_READ_RESP = 161
    CMD_PROCESS_RUN = 96
    CMD_PROCESS_RUN_ASYNC = 97
    CMD_PROCESS_POLL = 98
    CMD_PROCESS_KILL = 99
    CMD_PROCESS_RUN_RESP = 176
    CMD_PROCESS_RUN_ASYNC_RESP = 177
    CMD_PROCESS_POLL_RESP = 178
