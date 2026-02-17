"""Auto-generated protocol bindings. Do not edit manually."""
from __future__ import annotations
from construct import Int8ub, Int16ub, Int32ub, Int64ub, Struct as BinStruct  # type: ignore
from enum import IntEnum, StrEnum
from typing import Final

PROTOCOL_VERSION: Final[int] = 2
DEFAULT_BAUDRATE: Final[int] = 115200
MAX_PAYLOAD_SIZE: Final[int] = 64
DEFAULT_SAFE_BAUDRATE: Final[int] = 115200
MAX_FILEPATH_LENGTH: Final[int] = 64
MAX_DATASTORE_KEY_LENGTH: Final[int] = 32
DEFAULT_ACK_TIMEOUT_MS: Final[int] = 200
DEFAULT_RETRY_LIMIT: Final[int] = 5
MAX_PENDING_TX_FRAMES: Final[int] = 2
INVALID_ID_SENTINEL: Final[int] = 65535
CMD_FLAG_COMPRESSED: Final[int] = 32768
UINT8_MASK: Final[int] = 255
UINT16_MAX: Final[int] = 65535
PROCESS_DEFAULT_EXIT_CODE: Final[int] = 255
CRC32_MASK: Final[int] = 4294967295
CRC_INITIAL: Final[int] = 4294967295
CRC_POLYNOMIAL: Final[int] = 3988292384
FRAME_DELIMITER: Final[bytes] = bytes([0])
DIGITAL_LOW: Final[int] = 0
DIGITAL_HIGH: Final[int] = 1
RLE_ESCAPE_BYTE: Final[int] = 255
RLE_MIN_RUN_LENGTH: Final[int] = 4
RLE_MAX_RUN_LENGTH: Final[int] = 256
RLE_SINGLE_ESCAPE_MARKER: Final[int] = 255
STATUS_CODE_MIN: Final[int] = 48
STATUS_CODE_MAX: Final[int] = 63
SYSTEM_COMMAND_MIN: Final[int] = 64
SYSTEM_COMMAND_MAX: Final[int] = 79
GPIO_COMMAND_MIN: Final[int] = 80
GPIO_COMMAND_MAX: Final[int] = 95
CONSOLE_COMMAND_MIN: Final[int] = 96
CONSOLE_COMMAND_MAX: Final[int] = 111
DATASTORE_COMMAND_MIN: Final[int] = 112
DATASTORE_COMMAND_MAX: Final[int] = 127
MAILBOX_COMMAND_MIN: Final[int] = 128
MAILBOX_COMMAND_MAX: Final[int] = 143
FILESYSTEM_COMMAND_MIN: Final[int] = 144
FILESYSTEM_COMMAND_MAX: Final[int] = 159
PROCESS_COMMAND_MIN: Final[int] = 160
PROCESS_COMMAND_MAX: Final[int] = 175

HANDSHAKE_NONCE_LENGTH: Final[int] = 16
HANDSHAKE_TAG_LENGTH: Final[int] = 16
HANDSHAKE_ACK_TIMEOUT_MIN_MS: Final[int] = 25
HANDSHAKE_ACK_TIMEOUT_MAX_MS: Final[int] = 60000
HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS: Final[int] = 100
HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS: Final[int] = 180000
HANDSHAKE_RETRY_LIMIT_MIN: Final[int] = 1
HANDSHAKE_RETRY_LIMIT_MAX: Final[int] = 8
HANDSHAKE_HKDF_OUTPUT_LENGTH: Final[int] = 32
HANDSHAKE_NONCE_RANDOM_BYTES: Final[int] = 8
HANDSHAKE_NONCE_COUNTER_BYTES: Final[int] = 8
HANDSHAKE_TAG_ALGORITHM: Final[str] = "HMAC-SHA256"
HANDSHAKE_TAG_DESCRIPTION: Final[str] = "HMAC-SHA256(secret, nonce) truncated to 16 bytes"
HANDSHAKE_CONFIG_FORMAT: Final[str] = ">HBI"
HANDSHAKE_CONFIG_DESCRIPTION: Final[str] = (
    "Serialized timing config: ack_timeout_ms (uint16), ack_retry_limit (uint8), "
    "response_timeout_ms (uint32)"
)
HANDSHAKE_HKDF_ALGORITHM: Final[str] = "HKDF-SHA256"
HANDSHAKE_NONCE_FORMAT_DESCRIPTION: Final[str] = (
    "random[8] || counter[8] - counter is big-endian uint64 for anti-replay"
)
HANDSHAKE_HKDF_SALT: Final[bytes] = b"mcubridge-v2"
HANDSHAKE_HKDF_INFO_AUTH: Final[bytes] = b"handshake-auth"
HANDSHAKE_CONFIG_STRUCT: Final = BinStruct(
    "ack_timeout_ms" / Int16ub,
    "ack_retry_limit" / Int8ub,
    "response_timeout_ms" / Int32ub,
)
HANDSHAKE_CONFIG_SIZE: Final[int] = HANDSHAKE_CONFIG_STRUCT.sizeof()  # type: ignore

class CompressionType(IntEnum):
    NONE = 0
    RLE = 1


CAPABILITY_WATCHDOG: Final[int] = 1
CAPABILITY_RLE: Final[int] = 2
CAPABILITY_DEBUG_FRAMES: Final[int] = 4
CAPABILITY_DEBUG_IO: Final[int] = 8
CAPABILITY_EEPROM: Final[int] = 16
CAPABILITY_DAC: Final[int] = 32
CAPABILITY_HW_SERIAL1: Final[int] = 64
CAPABILITY_FPU: Final[int] = 128
CAPABILITY_LOGIC_3V3: Final[int] = 256
CAPABILITY_BIG_BUFFER: Final[int] = 512
CAPABILITY_I2C: Final[int] = 1024

DATASTORE_KEY_LEN_FORMAT: Final[str] = ">B"
DATASTORE_KEY_LEN_STRUCT: Final = Int8ub
DATASTORE_VALUE_LEN_FORMAT: Final[str] = ">B"
DATASTORE_VALUE_LEN_STRUCT: Final = Int8ub
CRC_COVERED_HEADER_FORMAT: Final[str] = ">BHH"
CRC_COVERED_HEADER_STRUCT: Final = BinStruct(
    "version" / Int8ub,
    "payload_len" / Int16ub,
    "command_id" / Int16ub,
)
CRC_FORMAT: Final[str] = ">I"
CRC_STRUCT: Final = Int32ub
UINT8_FORMAT: Final[str] = ">B"
UINT8_STRUCT: Final = Int8ub
UINT16_FORMAT: Final[str] = ">H"
UINT16_STRUCT: Final = Int16ub
UINT32_FORMAT: Final[str] = ">I"
UINT32_STRUCT: Final = Int32ub
PIN_READ_FORMAT: Final[str] = ">B"
PIN_READ_STRUCT: Final = Int8ub
PIN_WRITE_FORMAT: Final[str] = ">BB"
PIN_WRITE_STRUCT: Final = BinStruct(
    "pin" / Int8ub,
    "value" / Int8ub,
)
CAPABILITIES_FORMAT: Final[str] = ">BBBBI"
CAPABILITIES_STRUCT: Final = BinStruct(
    "ver" / Int8ub,
    "arch" / Int8ub,
    "dig" / Int8ub,
    "ana" / Int8ub,
    "feat" / Int32ub,
)
NONCE_COUNTER_FORMAT: Final[str] = ">Q"
NONCE_COUNTER_STRUCT: Final = Int64ub
DATASTORE_KEY_LEN_SIZE: Final[int] = DATASTORE_KEY_LEN_STRUCT.sizeof()  # type: ignore
DATASTORE_VALUE_LEN_SIZE: Final[int] = DATASTORE_VALUE_LEN_STRUCT.sizeof()  # type: ignore
CRC_COVERED_HEADER_SIZE: Final[int] = CRC_COVERED_HEADER_STRUCT.sizeof()  # type: ignore
CRC_SIZE: Final[int] = CRC_STRUCT.sizeof()  # type: ignore
MIN_FRAME_SIZE: Final[int] = CRC_COVERED_HEADER_SIZE + CRC_SIZE


MQTT_SUFFIX_INCOMING_AVAILABLE: Final[str] = "incoming_available"
MQTT_SUFFIX_OUTGOING_AVAILABLE: Final[str] = "outgoing_available"
MQTT_SUFFIX_RESPONSE: Final[str] = "response"
MQTT_SUFFIX_ERROR: Final[str] = "error"


MQTT_DEFAULT_TOPIC_PREFIX: Final[str] = "br"

STATUS_REASON_COMMAND_VALIDATION_FAILED: Final[str] = "command_validation_failed"
STATUS_REASON_INVALID_PATH: Final[str] = "invalid_path"
STATUS_REASON_MAILBOX_INCOMING_OVERFLOW: Final[str] = "mailbox_incoming_overflow"
STATUS_REASON_MAILBOX_OUTGOING_OVERFLOW: Final[str] = "mailbox_outgoing_overflow"
STATUS_REASON_PROCESS_KILL_FAILED: Final[str] = "process_kill_failed"
STATUS_REASON_PROCESS_KILL_MALFORMED: Final[str] = "process_kill_malformed"
STATUS_REASON_PROCESS_LIMIT_REACHED: Final[str] = "process_limit_reached"
STATUS_REASON_PROCESS_NOT_FOUND: Final[str] = "process_not_found"
STATUS_REASON_PROCESS_RUN_ASYNC_FAILED: Final[str] = "process_run_async_failed"
STATUS_REASON_PROCESS_RUN_INTERNAL_ERROR: Final[str] = "process_run_internal_error"
STATUS_REASON_READ_FAILED: Final[str] = "read_failed"
STATUS_REASON_REMOVE_FAILED: Final[str] = "remove_failed"
STATUS_REASON_WRITE_FAILED: Final[str] = "write_failed"

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
    INCOMING = "incoming"  # Mailbox incoming messages
    PROCESSED = "processed"  # Mailbox processed notifications
    ERRORS = "errors"  # Mailbox error topic


class DatastoreAction(StrEnum):
    GET = "get"  # Get datastore value
    PUT = "put"  # Put datastore value


class PinAction(StrEnum):
    MODE = "mode"  # Set pin mode
    READ = "read"  # Read pin value


class ConsoleAction(StrEnum):
    IN = "in"  # Console input
    OUT = "out"  # Console output
    INPUT = "input"  # Console input action


class SystemAction(StrEnum):
    FREE_MEMORY = "free_memory"  # System free memory
    VERSION = "version"  # System version
    GET = "get"  # Get system info
    VALUE = "value"  # Value payload segment
    METRICS = "metrics"  # Metrics topic
    BRIDGE = "bridge"  # Bridge snapshots
    HANDSHAKE = "handshake"  # Handshake snapshot
    SUMMARY = "summary"  # Bridge summary snapshot
    STATE = "state"  # Bridge state snapshot


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
    (Topic.SYSTEM, (SystemAction.BRIDGE.value, SystemAction.HANDSHAKE.value, SystemAction.GET.value,), 0),
    (Topic.SYSTEM, (SystemAction.BRIDGE.value, SystemAction.SUMMARY.value, SystemAction.GET.value,), 0),
    (Topic.SYSTEM, (SystemAction.BRIDGE.value, SystemAction.STATE.value, SystemAction.GET.value,), 0),
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
    CMD_GET_CAPABILITIES = 72
    CMD_GET_CAPABILITIES_RESP = 73
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

# Commands that expect a direct response without a prior ACK.
# The MCU responds directly with CMD_*_RESP without sending STATUS_ACK first.
RESPONSE_ONLY_COMMANDS: frozenset[int] = frozenset({
    Command.CMD_GET_VERSION.value,
    Command.CMD_GET_FREE_MEMORY.value,
    Command.CMD_GET_CAPABILITIES.value,
    Command.CMD_DIGITAL_READ.value,
    Command.CMD_ANALOG_READ.value,
})
