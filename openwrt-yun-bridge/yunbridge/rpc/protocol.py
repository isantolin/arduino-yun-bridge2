"""Auto-generated protocol bindings. Do not edit manually."""
from __future__ import annotations
import struct
from enum import IntEnum, StrEnum
from typing import Final

PROTOCOL_VERSION: Final[int] = 2
DEFAULT_BAUDRATE: Final[int] = 115200
DEFAULT_SAFE_BAUDRATE: Final[int] = 115200
MAX_PAYLOAD_SIZE: Final[int] = 128
RPC_BUFFER_SIZE: Final[int] = 128
MAX_FILEPATH_LENGTH: Final[int] = 64
MAX_DATASTORE_KEY_LENGTH: Final[int] = 32
DEFAULT_ACK_TIMEOUT_MS: Final[int] = 200
DEFAULT_RETRY_LIMIT: Final[int] = 5
MAX_PENDING_TX_FRAMES: Final[int] = 2
INVALID_ID_SENTINEL: Final[int] = 65535
RESPONSE_OFFSET: Final[int] = 128
UINT8_MASK: Final[int] = 255
UINT16_MAX: Final[int] = 65535
PROCESS_DEFAULT_EXIT_CODE: Final[int] = 255
CRC32_MASK: Final[int] = 4294967295
CRC_INITIAL: Final[int] = 4294967295
CRC_POLYNOMIAL: Final[int] = 3988292384
FRAME_DELIMITER: Final[bytes] = bytes([0])
DIGITAL_LOW: Final[int] = 0
DIGITAL_HIGH: Final[int] = 1

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

UINT8_FORMAT: Final[str] = ">B"
UINT16_FORMAT: Final[str] = ">H"
UINT32_FORMAT: Final[str] = ">I"
PIN_READ_FORMAT: Final[str] = ">B"
PIN_WRITE_FORMAT: Final[str] = ">BB"
DATASTORE_KEY_LEN_FORMAT: Final[str] = ">B"
DATASTORE_KEY_LEN_SIZE: Final[int] = struct.calcsize(DATASTORE_KEY_LEN_FORMAT)
DATASTORE_VALUE_LEN_FORMAT: Final[str] = ">B"
DATASTORE_VALUE_LEN_SIZE: Final[int] = struct.calcsize(DATASTORE_VALUE_LEN_FORMAT)
CRC_COVERED_HEADER_FORMAT: Final[str] = ">BHH"
CRC_COVERED_HEADER_SIZE: Final[int] = struct.calcsize(CRC_COVERED_HEADER_FORMAT)
CRC_FORMAT: Final[str] = ">I"
CRC_SIZE: Final[int] = struct.calcsize(CRC_FORMAT)
MIN_FRAME_SIZE: Final[int] = CRC_COVERED_HEADER_SIZE + CRC_SIZE


MQTT_SUFFIX_INCOMING_AVAILABLE: Final[str] = "incoming_available"
MQTT_SUFFIX_OUTGOING_AVAILABLE: Final[str] = "outgoing_available"
MQTT_SUFFIX_RESPONSE: Final[str] = "response"


class Topic(StrEnum):
    ANALOG = "a"  # Analog pin operations
    CONSOLE = "console"  # Remote console
    DATASTORE = "datastore"  # Key-value storage
    DIGITAL = "d"  # Digital pin operations
    FILE = "file"  # File system operations
    MAILBOX = "mailbox"  # Message passing
    SHELL = "sh"  # Shell command execution
    STATUS = "status"  # System status reporting
    SYSTEM = "system"  # System control and info


class Action(StrEnum):
    FILE_READ = "read"  # Read file content
    FILE_WRITE = "write"  # Write file content
    FILE_REMOVE = "remove"  # Remove file
    SHELL_RUN = "run"  # Run shell command
    SHELL_RUN_ASYNC = "run_async"  # Run shell command asynchronously
    SHELL_POLL = "poll"  # Poll shell command status
    SHELL_KILL = "kill"  # Kill shell command
    MAILBOX_WRITE = "write"  # Write to mailbox
    DATASTORE_GET = "get"  # Get datastore value
    DATASTORE_PUT = "put"  # Put datastore value
    PIN_MODE = "mode"  # Set pin mode
    PIN_READ = "read"  # Read pin value
    CONSOLE_IN = "in"  # Console input
    MAILBOX_READ = "read"  # Read from mailbox
    SYSTEM_FREE_MEMORY = "free_memory"  # System free memory
    SYSTEM_VERSION = "version"  # System version
    SYSTEM_GET = "get"  # Get system info
    DIGITAL_WRITE = "write"  # Digital write
    DIGITAL_READ = "read"  # Digital read
    DIGITAL_MODE = "mode"  # Digital mode
    ANALOG_WRITE = "write"  # Analog write
    ANALOG_READ = "read"  # Analog read
    CONSOLE_INPUT = "input"  # Console input action


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
    CMD_GET_VERSION = 10
    CMD_GET_VERSION_RESP = 128
    CMD_GET_FREE_MEMORY = 11
    CMD_GET_FREE_MEMORY_RESP = 130
    CMD_LINK_SYNC = 12
    CMD_LINK_SYNC_RESP = 131
    CMD_LINK_RESET = 13
    CMD_LINK_RESET_RESP = 132
    CMD_GET_TX_DEBUG_SNAPSHOT = 14
    CMD_GET_TX_DEBUG_SNAPSHOT_RESP = 133
    CMD_SET_BAUDRATE = 15
    CMD_SET_BAUDRATE_RESP = 134
    CMD_XOFF = 112
    CMD_XON = 113
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


ACK_ONLY_COMMANDS: frozenset[int] = frozenset({
    Command.CMD_SET_PIN_MODE.value,
    Command.CMD_DIGITAL_WRITE.value,
    Command.CMD_ANALOG_WRITE.value,
    Command.CMD_CONSOLE_WRITE.value,
    Command.CMD_DATASTORE_PUT.value,
})
