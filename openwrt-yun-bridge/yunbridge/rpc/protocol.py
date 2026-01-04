"""Auto-generated protocol bindings. Do not edit manually."""
from __future__ import annotations
import struct
from enum import IntEnum, StrEnum
from typing import Final

PROTOCOL_VERSION: Final[int] = 2
DEFAULT_BAUDRATE: Final[int] = 115200
DEFAULT_SAFE_BAUDRATE: Final[int] = 115200
MAX_PAYLOAD_SIZE: Final[int] = 128
MAX_FILEPATH_LENGTH: Final[int] = 64
MAX_DATASTORE_KEY_LENGTH: Final[int] = 32
DEFAULT_ACK_TIMEOUT_MS: Final[int] = 200
DEFAULT_RETRY_LIMIT: Final[int] = 5
MAX_PENDING_TX_FRAMES: Final[int] = 2
INVALID_ID_SENTINEL: Final[int] = 65535
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


MQTT_WILDCARD_SINGLE: Final[str] = "+"
MQTT_WILDCARD_MULTI: Final[str] = "#"


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


class FileAction(StrEnum):
    READ = "read"  # Read file content
    WRITE = "write"  # Write file content
    REMOVE = "remove"  # Remove file


class ShellAction(StrEnum):
    RUN = "run"  # Run shell command
    RUN_ASYNC = "run_async"  # Run shell command asynchronously
    POLL = "poll"  # Poll shell command status
    KILL = "kill"  # Kill shell command


class MailboxAction(StrEnum):
    WRITE = "write"  # Write to mailbox
    READ = "read"  # Read from mailbox


class DatastoreAction(StrEnum):
    GET = "get"  # Get datastore value
    PUT = "put"  # Put datastore value


class PinAction(StrEnum):
    MODE = "mode"  # Set pin mode
    READ = "read"  # Read pin value


class ConsoleAction(StrEnum):
    IN = "in"  # Console input
    INPUT = "input"  # Console input action


class SystemAction(StrEnum):
    FREE_MEMORY = "free_memory"  # System free memory
    VERSION = "version"  # System version
    TX_DEBUG = "tx_debug"  # System TX debug snapshot
    GET = "get"  # Get system info


class DigitalAction(StrEnum):
    WRITE = "write"  # Digital write
    READ = "read"  # Digital read
    MODE = "mode"  # Digital mode


class AnalogAction(StrEnum):
    WRITE = "write"  # Analog write
    READ = "read"  # Analog read


MQTT_COMMAND_SUBSCRIPTIONS: Final[tuple[tuple[Topic, tuple[str, ...], int], ...]] = (
    (Topic.DIGITAL, (MQTT_WILDCARD_SINGLE, DigitalAction.MODE.value,), 0),
    (Topic.DIGITAL, (MQTT_WILDCARD_SINGLE, DigitalAction.READ.value,), 0),
    (Topic.DIGITAL, (MQTT_WILDCARD_SINGLE,), 0),
    (Topic.ANALOG, (MQTT_WILDCARD_SINGLE, AnalogAction.READ.value,), 0),
    (Topic.ANALOG, (MQTT_WILDCARD_SINGLE,), 0),
    (Topic.CONSOLE, (ConsoleAction.IN.value,), 0),
    (Topic.DATASTORE, (DatastoreAction.PUT.value, MQTT_WILDCARD_MULTI,), 0),
    (Topic.DATASTORE, (DatastoreAction.GET.value, MQTT_WILDCARD_MULTI,), 0),
    (Topic.MAILBOX, (MailboxAction.WRITE.value,), 0),
    (Topic.MAILBOX, (MailboxAction.READ.value,), 0),
    (Topic.SHELL, (ShellAction.RUN.value,), 0),
    (Topic.SHELL, (ShellAction.RUN_ASYNC.value,), 0),
    (Topic.SHELL, (ShellAction.POLL.value, MQTT_WILDCARD_MULTI,), 0),
    (Topic.SHELL, (ShellAction.KILL.value, MQTT_WILDCARD_MULTI,), 0),
    (Topic.SYSTEM, (SystemAction.FREE_MEMORY.value, SystemAction.GET.value,), 0),
    (Topic.SYSTEM, (SystemAction.VERSION.value, SystemAction.GET.value,), 0),
    (Topic.SYSTEM, (SystemAction.TX_DEBUG.value, SystemAction.GET.value,), 0),
    (Topic.SYSTEM, ("bridge", "handshake", SystemAction.GET.value,), 0),
    (Topic.SYSTEM, ("bridge", "summary", SystemAction.GET.value,), 0),
    (Topic.SYSTEM, ("bridge", "state", SystemAction.GET.value,), 0),
    (Topic.FILE, (FileAction.WRITE.value, MQTT_WILDCARD_MULTI,), 0),
    (Topic.FILE, (FileAction.READ.value, MQTT_WILDCARD_MULTI,), 0),
    (Topic.FILE, (FileAction.REMOVE.value, MQTT_WILDCARD_MULTI,), 0),
)


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
