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
STATUS_CODE_MIN: Final[int] = 48
STATUS_CODE_MAX: Final[int] = 63
SYSTEM_COMMAND_MIN: Final[int] = 64
SYSTEM_COMMAND_MAX: Final[int] = 79
GPIO_COMMAND_MIN: Final[int] = 80

HANDSHAKE_NONCE_LENGTH: Final[int] = 16
HANDSHAKE_TAG_LENGTH: Final[int] = 16
HANDSHAKE_TAG_ALGORITHM: Final[str] = "HMAC-SHA256"
HANDSHAKE_TAG_DESCRIPTION: Final[str] = "HMAC-SHA256(secret, nonce) truncated to 16 bytes"
HANDSHAKE_CONFIG_FORMAT: Final[str] = ">HBI"
HANDSHAKE_CONFIG_DESCRIPTION: Final[str] = (
    "Serialized timing config: ack_timeout_ms (uint16), ack_retry_limit (uint8), "
    "response_timeout_ms (uint32)"
)
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


MQTT_COMMAND_SUBSCRIPTIONS: Final[tuple[tuple[Topic, tuple[str, ...], int], ...]] = (
    (Topic.DIGITAL, ("+", "mode",), 0),
    (Topic.DIGITAL, ("+", "read",), 0),
    (Topic.DIGITAL, ("+",), 0),
    (Topic.ANALOG, ("+", "read",), 0),
    (Topic.ANALOG, ("+",), 0),
    (Topic.CONSOLE, ("in",), 0),
    (Topic.DATASTORE, ("put", "#",), 0),
    (Topic.DATASTORE, ("get", "#",), 0),
    (Topic.MAILBOX, ("write",), 0),
    (Topic.MAILBOX, ("read",), 0),
    (Topic.SHELL, ("run",), 0),
    (Topic.SHELL, ("run_async",), 0),
    (Topic.SHELL, ("poll", "#",), 0),
    (Topic.SHELL, ("kill", "#",), 0),
    (Topic.SYSTEM, ("free_memory", "get",), 0),
    (Topic.SYSTEM, ("version", "get",), 0),
    (Topic.FILE, ("write", "#",), 0),
    (Topic.FILE, ("read", "#",), 0),
    (Topic.FILE, ("remove", "#",), 0),
)


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
    OK = 48  # Operation completed successfully.
    ERROR = 49  # Generic failure.
    CMD_UNKNOWN = 50  # Command not recognized.
    MALFORMED = 51  # Payload had invalid structure.
    OVERFLOW = 52  # Frame exceeded buffer size.
    CRC_MISMATCH = 53  # CRC check failed.
    TIMEOUT = 54  # Operation timed out.
    NOT_IMPLEMENTED = 55  # Command defined but not supported.
    ACK = 56  # Generic acknowledgement for fire-and-forget commands.


class Command(IntEnum):
    CMD_GET_VERSION = 64
    CMD_GET_VERSION_RESP = 65
    CMD_GET_FREE_MEMORY = 66
    CMD_GET_FREE_MEMORY_RESP = 67
    CMD_LINK_SYNC = 68
    CMD_LINK_SYNC_RESP = 69
    CMD_LINK_RESET = 70
    CMD_LINK_RESET_RESP = 71
    CMD_GET_TX_DEBUG_SNAPSHOT = 72
    CMD_GET_TX_DEBUG_SNAPSHOT_RESP = 73
    CMD_SET_BAUDRATE = 74
    CMD_SET_BAUDRATE_RESP = 75
    CMD_XOFF = 78
    CMD_XON = 79
    CMD_SET_PIN_MODE = 80
    CMD_DIGITAL_WRITE = 81
    CMD_ANALOG_WRITE = 82
    CMD_DIGITAL_READ = 83
    CMD_ANALOG_READ = 84
    CMD_DIGITAL_READ_RESP = 85
    CMD_ANALOG_READ_RESP = 86
    CMD_CONSOLE_WRITE = 96
    CMD_DATASTORE_PUT = 112
    CMD_DATASTORE_GET = 113
    CMD_DATASTORE_GET_RESP = 114
    CMD_MAILBOX_READ = 128
    CMD_MAILBOX_PROCESSED = 129
    CMD_MAILBOX_AVAILABLE = 130
    CMD_MAILBOX_PUSH = 131
    CMD_MAILBOX_READ_RESP = 132
    CMD_MAILBOX_AVAILABLE_RESP = 133
    CMD_FILE_WRITE = 144
    CMD_FILE_READ = 145
    CMD_FILE_REMOVE = 146
    CMD_FILE_READ_RESP = 147
    CMD_PROCESS_RUN = 160
    CMD_PROCESS_RUN_ASYNC = 161
    CMD_PROCESS_POLL = 162
    CMD_PROCESS_KILL = 163
    CMD_PROCESS_RUN_RESP = 164
    CMD_PROCESS_RUN_ASYNC_RESP = 165
    CMD_PROCESS_POLL_RESP = 166


ACK_ONLY_COMMANDS: frozenset[int] = frozenset({
    Command.CMD_SET_PIN_MODE.value,
    Command.CMD_DIGITAL_WRITE.value,
    Command.CMD_ANALOG_WRITE.value,
    Command.CMD_CONSOLE_WRITE.value,
    Command.CMD_DATASTORE_PUT.value,
    Command.CMD_MAILBOX_PUSH.value,
    Command.CMD_FILE_WRITE.value,
})
