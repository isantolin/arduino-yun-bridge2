"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Msgspec (SIL-2).
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Iterable
from enum import IntEnum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    Self,
    Type,
    TypeVar,
    cast,
)

import google.protobuf.message
import msgspec
from mcubridge.protocol import mcubridge_pb2 as mcubridge_pb2

if TYPE_CHECKING:
    from mcubridge.policy import AllowedCommandPolicy, TopicAuthorization


from construct import BitStruct, Flag, Padding, Construct

# [SIL-2] Declarative bitmask definition for MCU capabilities.
# This ensures atomic bit-level parsing/building via Construct's C-backed engine.
# Order matches the protocol specification (bit 0 to bit 15).
FEATURES_STRUCT: Final = cast(Construct, BitStruct(
    cast(Construct, "sd" / Flag),
    cast(Construct, "spi" / Flag),
    cast(Construct, "i2c" / Flag),
    cast(Construct, "big_buffer" / Flag),
    cast(Construct, "logic_3v3" / Flag),
    cast(Construct, "fpu" / Flag),
    cast(Construct, "hw_serial1" / Flag),
    cast(Construct, "dac" / Flag),
    cast(Construct, "eeprom" / Flag),
    cast(Construct, "debug_io" / Flag),
    cast(Construct, "debug_frames" / Flag),
    cast(Construct, "rle" / Flag),
    cast(Construct, "watchdog" / Flag),
    cast(Construct, Padding(3)),
))


def _capabilities_to_int(feat_dict: dict[str, Any]) -> int:
    """Convert a capability feature dict to its integer bitmask using Construct."""
    try:
        # Build raw bytes from dict and parse back as 16-bit integer
        from construct import Int16ul
        return int(Int16ul.parse(FEATURES_STRUCT.build(feat_dict)))
    except Exception:
        return 0


def _int_to_capabilities(val: int) -> dict[str, bool]:
    """Convert an integer bitmask to a capability feature dict using Construct."""
    try:
        from construct import Int16ul
        # Convert integer to bytes then parse via BitStruct
        data = Int16ul.build(int(val))
        res: Any = FEATURES_STRUCT.parse(data)
        # Convert Container to plain dict and remove internal metadata
        return {
            str(k): bool(v)
            for k, v in dict(res).items()
            if not str(k).startswith("_")
        }
    except Exception:
        return {}


# =============================================================================
# 1. Protocol Generation Structures (re-exported from spec_model)
# =============================================================================

from .spec_model import (  # noqa: E402, F401
    CommandDef as CommandDef,
    ProtocolSpec as ProtocolSpec,
    RawProtocolData as RawProtocolData,
    StatusDef as StatusDef,
)


# =============================================================================
# 2. Security and Policy Structures (msgspec)
# =============================================================================


class AllowedCommandPolicy(msgspec.Struct, frozen=True):
    """Normalised allow-list for shell/process commands."""

    entries: tuple[str, ...]

    @property
    def allow_all(self) -> bool:
        from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD

        return ALLOWED_COMMAND_WILDCARD in self.entries

    def is_allowed(self, command: str) -> bool:
        import fnmatch

        pieces = command.strip().split()
        if not pieces:
            return False
        return self.allow_all or any(fnmatch.fnmatch(pieces[0].lower(), p) for p in self.entries)

    def __contains__(self, item: str) -> bool:
        return item.lower() in self.entries

    def as_tuple(self) -> tuple[str, ...]:
        return self.entries

    @classmethod
    def from_iterable(
        cls,
        entries: Iterable[str],
    ) -> AllowedCommandPolicy:
        from mcubridge.util import normalise_allowed_commands

        normalised = normalise_allowed_commands(entries)
        return cls(entries=normalised)

    @classmethod
    def create_empty(cls) -> AllowedCommandPolicy:
        """Create an empty policy with no allowed commands."""
        return cls(entries=())


class TopicAuthorization(msgspec.Struct, frozen=True):
    """Per-topic allow flags for MQTT-driven actions.

    Optimized for lookup speed using a pre-calculated frozenset of allowed (topic, action) tuples.
    """

    file_read: bool = True
    file_write: bool = True
    file_remove: bool = True
    datastore_get: bool = True
    datastore_put: bool = True
    mailbox_read: bool = True
    mailbox_write: bool = True
    shell_run_async: bool = True
    shell_poll: bool = True
    shell_kill: bool = True
    console_input: bool = True
    digital_write: bool = True
    digital_read: bool = True
    digital_mode: bool = True
    analog_write: bool = True
    analog_read: bool = True
    system_version: bool = True
    system_free_memory: bool = True
    system_bootloader: bool = True
    spi_begin: bool = True
    spi_end: bool = True
    spi_transfer: bool = True
    spi_config: bool = True

    # Cache for allowed permissions (not serialized)
    _allowed_cache: Final[frozenset[tuple[str, str]]] = frozenset()

    def __post_init__(self) -> None:
        """Build the optimized lookup cache."""
        from mcubridge.protocol.protocol import (
            AnalogAction,
            ConsoleAction,
            DatastoreAction,
            DigitalAction,
            FileAction,
            MailboxAction,
            ShellAction,
            SpiAction,
            SystemAction,
        )
        from mcubridge.protocol.topics import Topic

        # Static mapping to avoid recreation in __post_init__
        _TOPIC_AUTH_MAPPING: Final[dict[tuple[str, str], str]] = {
            (Topic.FILE.value, FileAction.READ.value): "file_read",
            (Topic.FILE.value, FileAction.WRITE.value): "file_write",
            (Topic.FILE.value, FileAction.REMOVE.value): "file_remove",
            (Topic.DATASTORE.value, DatastoreAction.GET.value): "datastore_get",
            (Topic.DATASTORE.value, DatastoreAction.PUT.value): "datastore_put",
            (Topic.MAILBOX.value, MailboxAction.READ.value): "mailbox_read",
            (Topic.MAILBOX.value, MailboxAction.WRITE.value): "mailbox_write",
            (Topic.SHELL.value, ShellAction.RUN_ASYNC.value): "shell_run_async",
            (Topic.SHELL.value, ShellAction.POLL.value): "shell_poll",
            (Topic.SHELL.value, ShellAction.KILL.value): "shell_kill",
            (Topic.CONSOLE.value, ConsoleAction.IN.value): "console_input",
            (Topic.CONSOLE.value, ConsoleAction.INPUT.value): "console_input",
            (Topic.DIGITAL.value, DigitalAction.WRITE.value): "digital_write",
            (Topic.DIGITAL.value, DigitalAction.READ.value): "digital_read",
            (Topic.DIGITAL.value, DigitalAction.MODE.value): "digital_mode",
            (Topic.ANALOG.value, AnalogAction.WRITE.value): "analog_write",
            (Topic.ANALOG.value, AnalogAction.READ.value): "analog_read",
            (Topic.SYSTEM.value, SystemAction.VERSION.value): "system_version",
            (Topic.SYSTEM.value, SystemAction.FREE_MEMORY.value): "system_free_memory",
            (Topic.SYSTEM.value, SystemAction.BOOTLOADER.value): "system_bootloader",
            (Topic.SPI.value, SpiAction.BEGIN.value): "spi_begin",
            (Topic.SPI.value, SpiAction.END.value): "spi_end",
            (Topic.SPI.value, SpiAction.TRANSFER.value): "spi_transfer",
            (Topic.SPI.value, SpiAction.CONFIG.value): "spi_config",
        }

        allowed = [k for k, a in _TOPIC_AUTH_MAPPING.items() if getattr(self, a)]
        object.__setattr__(self, "_allowed_cache", frozenset(allowed))

    def allows(self, topic: str, action: str) -> bool:
        """Check if action is allowed on topic. O(1) complexity."""
        return (topic.lower(), action.lower()) in self._allowed_cache


# =============================================================================
# 3. Runtime Configuration Structures (msgspec)
# =============================================================================


class RuntimeConfig(msgspec.Struct, kw_only=True):
    """Strongly typed configuration for the daemon."""

    # Imports moved inside __post_init__ or methods to avoid circularity
    # but we need constants for defaults.
    from mcubridge.config.const import (
        DEFAULT_ALLOW_NON_TMP_PATHS,
        DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
        DEFAULT_BRIDGE_SUMMARY_INTERVAL,
        DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        DEFAULT_DEBUG_LOGGING,
        DEFAULT_FILE_STORAGE_QUOTA_BYTES,
        DEFAULT_FILE_SYSTEM_ROOT,
        DEFAULT_FILE_WRITE_MAX_BYTES,
        DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        DEFAULT_MAILBOX_QUEUE_LIMIT,
        DEFAULT_METRICS_ENABLED,
        DEFAULT_METRICS_HOST,
        DEFAULT_METRICS_PORT,
        DEFAULT_MQTT_CAFILE,
        DEFAULT_MQTT_HOST,
        DEFAULT_MQTT_PORT,
        DEFAULT_MQTT_QUEUE_LIMIT,
        DEFAULT_MQTT_SPOOL_DIR,
        DEFAULT_MQTT_TLS_INSECURE,
        DEFAULT_PENDING_PIN_REQUESTS,
        DEFAULT_PROCESS_MAX_CONCURRENT,
        DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
        DEFAULT_PROCESS_TIMEOUT,
        DEFAULT_RECONNECT_DELAY,
        DEFAULT_SERIAL_FALLBACK_THRESHOLD,
        DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
        DEFAULT_SERIAL_PORT,
        DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        DEFAULT_SERIAL_RETRY_TIMEOUT,
        DEFAULT_SERIAL_SHARED_SECRET,
        DEFAULT_STATUS_INTERVAL,
        DEFAULT_WATCHDOG_INTERVAL,
        MIN_SERIAL_SHARED_SECRET_LEN,
    )
    from mcubridge.protocol.protocol import (
        DEFAULT_BAUDRATE,
        DEFAULT_RETRY_LIMIT,
        DEFAULT_SAFE_BAUDRATE,
        MQTT_DEFAULT_TOPIC_PREFIX,
    )

    serial_port: str = DEFAULT_SERIAL_PORT
    serial_baud: Annotated[int, msgspec.Meta(ge=300)] = DEFAULT_BAUDRATE
    serial_safe_baud: Annotated[int, msgspec.Meta(ge=300)] = DEFAULT_SAFE_BAUDRATE
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: Annotated[int, msgspec.Meta(ge=1, le=65535)] = DEFAULT_MQTT_PORT
    mqtt_user: str | None = None
    mqtt_pass: str | None = None
    mqtt_tls: bool = True
    mqtt_cafile: str | None = DEFAULT_MQTT_CAFILE
    mqtt_certfile: str | None = None
    mqtt_keyfile: str | None = None
    mqtt_topic: str = MQTT_DEFAULT_TOPIC_PREFIX

    # [SIL-2] Accept Any to allow raw strings from UCI/Tests, then coerce in __post_init__
    allowed_commands: Any = ()

    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_TIMEOUT

    mqtt_tls_insecure: bool = DEFAULT_MQTT_TLS_INSECURE
    file_write_max_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_STORAGE_QUOTA_BYTES

    allowed_policy: AllowedCommandPolicy | None = None

    mqtt_queue_limit: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_MQTT_QUEUE_LIMIT
    reconnect_delay: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_RECONNECT_DELAY
    status_interval: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_STATUS_INTERVAL
    debug_logging: bool = msgspec.field(default=DEFAULT_DEBUG_LOGGING, name="debug")
    console_queue_limit_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PENDING_PIN_REQUESTS
    serial_retry_timeout: Annotated[float, msgspec.Meta(ge=0.01)] = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: Annotated[float, msgspec.Meta(ge=0.02)] = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_RETRY_LIMIT
    serial_fallback_threshold: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_FALLBACK_THRESHOLD
    serial_handshake_min_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    serial_handshake_fatal_failures: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    mqtt_enabled: bool = True
    watchdog_enabled: bool = True
    watchdog_interval: Annotated[float, msgspec.Meta(ge=0.1)] = DEFAULT_WATCHDOG_INTERVAL
    topic_authorization: TopicAuthorization | None = None

    # [SIL-2] Security: Accept Any to allow raw strings from UCI/Tests,
    # then coerce to bytes in __post_init__ to avoid msgspec base64 errors.
    serial_shared_secret: Any = DEFAULT_SERIAL_SHARED_SECRET

    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    process_max_output_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_CONCURRENT
    metrics_enabled: bool = DEFAULT_METRICS_ENABLED
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: Annotated[int, msgspec.Meta(ge=1, le=65535)] = DEFAULT_METRICS_PORT
    bridge_summary_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_SUMMARY_INTERVAL
    bridge_handshake_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_HANDSHAKE_INTERVAL
    allow_non_tmp_paths: bool = DEFAULT_ALLOW_NON_TMP_PATHS

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls

    def __post_init__(self) -> None:
        from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET, VOLATILE_STORAGE_PATHS

        # [SIL-2] Automated Normalization: Strip and clean inputs
        self.serial_port = self.serial_port.strip()
        self.mqtt_host = self.mqtt_host.strip()
        self.file_system_root = str(Path(self.file_system_root).expanduser().resolve())
        self.mqtt_spool_dir = str(Path(self.mqtt_spool_dir).expanduser().resolve())

        # Support single string for allowed_commands (common in tests/UCI)
        if isinstance(self.allowed_commands, str):
            self.allowed_commands = tuple(self.allowed_commands.split())

        # Normalize Optional strings
        if self.mqtt_user is not None:
            self.mqtt_user = self.mqtt_user.strip() or None
        if self.mqtt_pass is not None:
            self.mqtt_pass = self.mqtt_pass.strip() or None
        if self.mqtt_cafile is not None:
            self.mqtt_cafile = self.mqtt_cafile.strip() or None
        if self.mqtt_certfile is not None:
            self.mqtt_certfile = self.mqtt_certfile.strip() or None
        if self.mqtt_keyfile is not None:
            self.mqtt_keyfile = self.mqtt_keyfile.strip() or None

        # [SIL-2] MQTT Topic Normalization
        from mcubridge.protocol.topics import split_topic_segments

        raw_topic = str(self.mqtt_topic).strip()
        segments = split_topic_segments(raw_topic)
        if not segments:
            raise ValueError("mqtt_topic must contain at least one segment")
        self.mqtt_topic = "/".join(segments)

        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        self.allowed_commands = self.allowed_policy.entries if self.allowed_policy else ()
        if self.topic_authorization is None or isinstance(self.topic_authorization, dict):
            self.topic_authorization = (
                msgspec.convert(self.topic_authorization, TopicAuthorization)
                if self.topic_authorization
                else TopicAuthorization()
            )

        # [SIL-2] Coerce secret to bytes
        if isinstance(self.serial_shared_secret, str):
            self.serial_shared_secret = self.serial_shared_secret.strip().encode("utf-8")

        # [SIL-2] Strict Semantic Validations
        if self.serial_response_timeout < self.serial_retry_timeout * 2:
            raise ValueError("serial_response_timeout must be at least 2x serial_retry_timeout")

        if self.watchdog_enabled and self.watchdog_interval < 0.5:
            raise ValueError("watchdog_interval must be >= 0.5s when enabled")

        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")

        if self.serial_shared_secret == b"changeme123":
            raise ValueError("serial_shared_secret placeholder is insecure")

        # Unique symbol check for minimum entropy
        if isinstance(self.serial_shared_secret, bytes):
            unique_symbols = {byte for byte in self.serial_shared_secret}
            if len(unique_symbols) < 4 and self.serial_shared_secret != DEFAULT_SERIAL_SHARED_SECRET:
                raise ValueError("serial_shared_secret must contain at least four distinct bytes")

        # Logic-based cross-field validations
        if self.file_storage_quota_bytes < self.file_write_max_bytes:
            raise ValueError("file_storage_quota_bytes must be greater than or equal to file_write_max_bytes")

        if self.mailbox_queue_bytes_limit < self.mailbox_queue_limit:
            raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")

        # [SIL-2] Flash Protection: Spooling must ALWAYS be in volatile RAM.
        if not any(self.mqtt_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
            raise ValueError(
                f"FLASH PROTECTION: mqtt_spool_dir ({self.mqtt_spool_dir}) must be in a volatile location (e.g. /tmp)"
            )

        if not self.allow_non_tmp_paths:
            if not any(self.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(
                    f"FLASH PROTECTION: file_system_root ({self.file_system_root}) must be in a volatile location"
                )


# =============================================================================
# 3. Operational Structures
# =============================================================================

T = TypeVar("T", bound="BaseStruct")


class ProtobufCompatSchema:
    def __init__(self, pb_class: type, struct_class: type):
        self.pb_class = pb_class
        self.struct_class = struct_class

    def build(self, obj: dict[str, Any] | msgspec.Struct) -> bytes:
        from google.protobuf.json_format import ParseDict
        pb_obj = self.pb_class()
        if isinstance(obj, dict):
            # Shallow copy to avoid modifying original
            d: dict[str, Any] = dict(obj)
            # Special case for Capabilities feat mapping if passed as dict
            if self.struct_class.__name__ == "CapabilitiesPacket" and "feat" in d:
                d["feat"] = _capabilities_to_int(d["feat"])
            ParseDict(d, pb_obj)
        else:
            # If it's already a msgspec struct
            d: dict[str, Any] = msgspec.structs.asdict(obj)
            # handle nested CapabilitiesFeatures
            if self.struct_class.__name__ == "CapabilitiesPacket" and "feat" in d:
                d["feat"] = _capabilities_to_int(d["feat"])
            ParseDict(d, pb_obj)
        return pb_obj.SerializeToString()

    def parse(self, data: bytes) -> dict[str, Any]:
        pb_obj = self.pb_class()
        pb_obj.ParseFromString(bytes(data))
        from google.protobuf.json_format import MessageToDict
        d = MessageToDict(pb_obj, preserving_proto_field_name=True, use_integers_for_enums=True)
        # Handle CapabilitiesPacket bitset mapping
        if self.struct_class.__name__ == "CapabilitiesPacket" and "feat" in d:
            d["feat"] = _int_to_capabilities(d["feat"])
        return d

    def sizeof(self) -> int:
        # Protobuf size is variable, but for compatibility we can return a representative size
        # or the max size if known.
        return 64 # MAX_PAYLOAD_SIZE fallback


class BaseStruct(msgspec.Struct, frozen=True):
    """Base class for hybrid Msgspec/Nanopb structures."""

    PB_CLASS: ClassVar[type]

    # Re-implementing SCHEMA as a class property for Python 3.13 compatibility
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if hasattr(cls, "PB_CLASS"):
            cls.SCHEMA = ProtobufCompatSchema(cls.PB_CLASS, cls) # type: ignore

    @classmethod
    def decode(cls: Type[T], data: bytes | bytearray | memoryview, command_id: int | None = None) -> T:
        try:
            pb_obj = cls.PB_CLASS()
            pb_obj.ParseFromString(bytes(data))
            from google.protobuf.json_format import MessageToDict
            d = MessageToDict(
                pb_obj,
                preserving_proto_field_name=True,
                use_integers_for_enums=True,
                always_print_fields_with_no_presence=True,
            )
            if cls.__name__ == "CapabilitiesPacket" and "feat" in d:
                d["feat"] = _int_to_capabilities(int(d["feat"]))
            return msgspec.convert(d, cls)
        except (
            msgspec.MsgspecError,
            google.protobuf.message.Error,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
        ) as e:
            raise ValueError(f"Malformed {cls.__name__} payload: {bytes(data).hex()} - Error: {e}") from e

    def encode(self) -> bytes:
        pb_obj = self.PB_CLASS()
        # [SIL-2] Direct attribute mapping from msgspec to protobuf descriptors.
        # This avoids creating an intermediate dictionary and uses direct memory access.
        for field in pb_obj.DESCRIPTOR.fields:
            val = getattr(self, field.name, None)
            if val is not None:
                if self.__class__.__name__ == "CapabilitiesPacket" and field.name == "feat":
                    setattr(pb_obj, field.name, _capabilities_to_int(val))
                else:
                    setattr(pb_obj, field.name, val)
        return pb_obj.SerializeToString()


# --- Binary Protocol Packets ---


class FileWritePacket(BaseStruct, frozen=True):
    path: str
    data: bytes

    PB_CLASS = mcubridge_pb2.FileWrite


class FileReadPacket(BaseStruct, frozen=True):
    path: str

    PB_CLASS = mcubridge_pb2.FileRead


class FileReadResponsePacket(BaseStruct, frozen=True):
    content: bytes

    PB_CLASS = mcubridge_pb2.FileReadResponse


class FileRemovePacket(BaseStruct, frozen=True):
    path: str

    PB_CLASS = mcubridge_pb2.FileRemove


class VersionResponsePacket(BaseStruct, frozen=True):
    major: Annotated[int, msgspec.Meta(ge=0)]
    minor: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.VersionResponse


class FreeMemoryResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.FreeMemoryResponse


class DigitalReadResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.DigitalReadResponse


class AnalogReadResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.AnalogReadResponse


class DatastoreGetPacket(BaseStruct, frozen=True):
    key: str

    PB_CLASS = mcubridge_pb2.DatastoreGet


class DatastoreGetResponsePacket(BaseStruct, frozen=True):
    value: bytes

    PB_CLASS = mcubridge_pb2.DatastoreGetResponse


class DatastorePutPacket(BaseStruct, frozen=True):
    key: str
    value: bytes

    PB_CLASS = mcubridge_pb2.DatastorePut


class MailboxPushPacket(BaseStruct, frozen=True):
    data: bytes

    PB_CLASS = mcubridge_pb2.MailboxPush


class MailboxProcessedPacket(BaseStruct, frozen=True):
    message_id: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.MailboxProcessed


class MailboxAvailableResponsePacket(BaseStruct, frozen=True):
    count: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.MailboxAvailableResponse


class MailboxReadResponsePacket(BaseStruct, frozen=True):
    content: bytes

    PB_CLASS = mcubridge_pb2.MailboxReadResponse


class PinModePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    mode: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.PinMode


class DigitalWritePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.DigitalWrite


class AnalogWritePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.AnalogWrite


class PinReadPacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.PinRead


class AckPacket(BaseStruct, frozen=True):
    command_id: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.AckPacket


class ConsoleWritePacket(BaseStruct, frozen=True):
    data: bytes

    PB_CLASS = mcubridge_pb2.ConsoleWrite


class ProcessRunAsyncPacket(BaseStruct, frozen=True):
    command: str

    PB_CLASS = mcubridge_pb2.ProcessRunAsync


class ProcessKillPacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.ProcessKill


class ProcessPollPacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.ProcessPoll


class ProcessRunAsyncResponsePacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.ProcessRunAsyncResponse


class ProcessPollResponsePacket(BaseStruct, frozen=True):
    status: Annotated[int, msgspec.Meta(ge=0)]
    exit_code: Annotated[int, msgspec.Meta(ge=0)]
    stdout_data: bytes
    stderr_data: bytes

    PB_CLASS = mcubridge_pb2.ProcessPollResponse


class HandshakeConfigPacket(BaseStruct, frozen=True):
    ack_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]
    ack_retry_limit: Annotated[int, msgspec.Meta(ge=0)]
    response_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.HandshakeConfig


class CapabilitiesFeatures(msgspec.Struct, frozen=True):
    """Features bitmask parsed via BitStruct."""

    watchdog: bool
    rle: bool
    debug_frames: bool
    debug_io: bool
    eeprom: bool
    dac: bool
    hw_serial1: bool
    fpu: bool
    logic_3v3: bool
    big_buffer: bool
    i2c: bool
    spi: bool
    sd: bool


class CapabilitiesPacket(BaseStruct, frozen=True):
    ver: Annotated[int, msgspec.Meta(ge=0)]
    arch: Annotated[int, msgspec.Meta(ge=0)]
    dig: Annotated[int, msgspec.Meta(ge=0)]
    ana: Annotated[int, msgspec.Meta(ge=0)]
    feat: CapabilitiesFeatures

    PB_CLASS = mcubridge_pb2.Capabilities


class SetBaudratePacket(BaseStruct, frozen=True):
    baudrate: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.SetBaudratePacket


class EnterBootloaderPacket(BaseStruct, frozen=True):
    magic: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.EnterBootloader


class SpiTransferPacket(BaseStruct, frozen=True):
    data: bytes

    PB_CLASS = mcubridge_pb2.SpiTransfer


class SpiTransferResponsePacket(BaseStruct, frozen=True):
    data: bytes

    PB_CLASS = mcubridge_pb2.SpiTransferResponse


class SpiConfigPacket(BaseStruct, frozen=True):
    bit_order: Annotated[int, msgspec.Meta(ge=0)]
    data_mode: Annotated[int, msgspec.Meta(ge=0)]
    frequency: Annotated[int, msgspec.Meta(ge=0)]

    PB_CLASS = mcubridge_pb2.SpiConfig


# [SIL-2] Payload Schema Map: Centralized registry for all command payloads.
# This eliminates manual if/elif dispatching across components.

# --- High-Level Structure (Msgspec Only) ---


class MqttPayload(msgspec.Struct, frozen=True):
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False
    properties: dict[str, Any] = {}


class PinRequest(msgspec.Struct, frozen=True):
    pin: int
    state: str


class ServiceHealth(msgspec.Struct, frozen=True):
    name: str
    status: str
    restarts: int
    last_failure_unix: float
    last_exception: str | None = None


class SystemStatus(msgspec.Struct, frozen=True):
    cpu_percent: float | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    load_avg_1m: float | None
    uptime_seconds: float


# --- MQTT Spool Structures ---


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


UserProperty = tuple[str, str]


class SpoolRecord(msgspec.Struct, omit_defaults=True):
    """JSON-serializable record stored in the durable spool (RAM/Disk)."""

    topic_name: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: list[UserProperty] = msgspec.field(default_factory=list[tuple[str, str]])


class QueuedPublish(msgspec.Struct):
    """Serializable MQTT publish packet used by the durable spool."""

    topic_name: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: list[UserProperty] = msgspec.field(default_factory=list[tuple[str, str]])

    def to_record(self) -> SpoolRecord:
        """Convert to a QueuedPublish to SpoolRecord for serialization."""
        return SpoolRecord(
            topic_name=self.topic_name,
            payload=self.payload,
            qos=int(self.qos),
            retain=self.retain,
            content_type=self.content_type,
            payload_format_indicator=self.payload_format_indicator,
            message_expiry_interval=self.message_expiry_interval,
            response_topic=self.response_topic,
            correlation_data=self.correlation_data,
            user_properties=self.user_properties,
        )

    @classmethod
    def from_record(cls, record: SpoolRecord | dict[str, Any]) -> Self:
        """Create a QueuedPublish instance from a SpoolRecord struct or dict."""
        def dec_hook(target_type: Type[Any], obj: Any) -> Any:
            if target_type is bytes and isinstance(obj, str):
                try:
                    return base64.b64decode(obj)
                except ValueError:
                    return obj.encode("utf-8")
            return obj

        # [SIL-2] Bulk conversion with hook delegates normalization to library
        data = record if isinstance(record, dict) else msgspec.structs.asdict(record)
        return msgspec.convert(data, cls, dec_hook=dec_hook)


# --- Process Service Structures ---


class ProcessOutputBatch(msgspec.Struct):
    """Structured payload describing PROCESS_POLL results."""

    status_byte: int
    exit_code: int
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool


# --- Queue Structures ---


class QueueEvent(msgspec.Struct):
    """Outcome of a bounded queue mutation."""

    truncated_bytes: int = 0
    dropped_chunks: int = 0
    dropped_bytes: int = 0
    accepted: bool = False


# --- Serial Flow Structures ---


class PendingCommand(msgspec.Struct):
    """Book-keeping for a tracked command in flight."""

    command_id: int
    expected_resp_ids: set[int] = msgspec.field(default_factory=lambda: set[int]())  # noqa: PLW0108
    completion: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False
    reply_topic: str | None = None
    correlation_data: bytes | None = None

    def mark_success(self) -> None:
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        if not self.completion.is_set():
            self.completion.set()


# --- Status Structures ---


class BaseStats(msgspec.Struct):
    """Base for statistics containers providing standard dict conversion."""

    def as_dict(self) -> dict[str, Any]:
        """Export internal state as a dictionary."""
        return msgspec.structs.asdict(self)


class SupervisorSnapshot(msgspec.Struct):
    restarts: Annotated[int, msgspec.Meta(ge=0)]
    last_failure_unix: float
    last_exception: str | None
    backoff_seconds: Annotated[float, msgspec.Meta(ge=0.0)]
    fatal: bool


class SupervisorStats(BaseStats):
    """Task supervisor statistics."""

    restarts: int = 0
    last_failure_unix: float = 0.0
    last_exception: str | None = None
    backoff_seconds: float = 0.0
    fatal: bool = False

    def as_snapshot(self) -> SupervisorSnapshot:
        return SupervisorSnapshot(
            restarts=self.restarts,
            last_failure_unix=self.last_failure_unix,
            last_exception=self.last_exception,
            backoff_seconds=self.backoff_seconds,
            fatal=self.fatal,
        )


_ARCH_MAPPING: Final[dict[int, str]] = {
    1: "Atmel AVR",
    2: "Espressif ESP32",
    3: "Espressif ESP8266",
    4: "Microchip SAMD",
    5: "Microchip SAM",
    6: "Raspberry Pi RP2040",
}


class McuCapabilities(msgspec.Struct):
    """Hardware capabilities reported by the MCU."""

    protocol_version: int = 0
    board_arch: int = 0
    num_digital_pins: int = 0
    num_analog_inputs: int = 0
    features: CapabilitiesFeatures | None = None

    @property
    def arch_name(self) -> str:
        return _ARCH_MAPPING.get(self.board_arch, f"Unknown (0x{self.board_arch:02X})")

    def _has(self, feat: str) -> bool:
        return bool(self.features and getattr(self.features, feat, False))

    @property
    def has_watchdog(self) -> bool:
        return self._has("watchdog")

    @property
    def has_rle(self) -> bool:
        return self._has("rle")

    @property
    def has_debug_frames(self) -> bool:
        return self._has("debug_frames")

    @property
    def has_debug_io(self) -> bool:
        return self._has("debug_io")

    @property
    def has_eeprom(self) -> bool:
        return self._has("eeprom")

    @property
    def has_dac(self) -> bool:
        return self._has("dac")

    @property
    def has_hw_serial1(self) -> bool:
        return self._has("hw_serial1")

    @property
    def has_fpu(self) -> bool:
        return self._has("fpu")

    @property
    def is_3v3_logic(self) -> bool:
        return self._has("logic_3v3")

    @property
    def has_big_buffer(self) -> bool:
        return self._has("big_buffer")

    @property
    def has_i2c(self) -> bool:
        return self._has("i2c")

    @property
    def has_spi(self) -> bool:
        return self._has("spi")

    @property
    def has_sd(self) -> bool:
        return self._has("sd")

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary including expanded boolean flags."""
        res = msgspec.structs.asdict(self)
        fields = (
            "has_watchdog",
            "has_rle",
            "has_debug_frames",
            "has_debug_io",
            "has_eeprom",
            "has_dac",
            "has_hw_serial1",
            "has_fpu",
            "is_3v3_logic",
            "has_big_buffer",
            "has_i2c",
            "has_spi",
            "has_sd",
        )
        for f in fields:
            res[f] = getattr(self, f)
        return res


class SerialThroughputStats(BaseStats):
    """Serial link throughput counters."""

    bytes_sent: int = 0
    bytes_received: int = 0
    frames_sent: int = 0
    frames_received: int = 0
    last_tx_unix: float = 0.0
    last_rx_unix: float = 0.0

    def record_tx(self, nbytes: int) -> None:
        self.bytes_sent += nbytes
        self.frames_sent += 1
        self.last_tx_unix = time.time()

    def record_rx(self, nbytes: int) -> None:
        self.bytes_received += nbytes
        self.frames_received += 1
        self.last_rx_unix = time.time()


# [EXTENDED METRICS] Latency histogram bucket boundaries in milliseconds
LATENCY_BUCKETS_MS: tuple[float, ...] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
)


class SerialLatencyStats(msgspec.Struct):
    """RPC command latency histogram."""

    bucket_counts: list[int] = msgspec.field(default_factory=lambda: [0] * len(LATENCY_BUCKETS_MS))
    overflow_count: int = 0
    total_observations: int = 0
    total_latency_ms: float = 0.0
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0
    _summary: Any | None = None  # Prometheus Summary

    def initialize_prometheus(self, registry: Any | None = None) -> None:
        from prometheus_client import Summary

        self._summary = Summary(
            "mcubridge_rpc_latency_seconds",
            "RPC command round-trip latency",
            registry=registry,
        )

    def record(self, latency_ms: float) -> None:
        self.total_observations += 1
        self.total_latency_ms += latency_ms
        if latency_ms < self.min_latency_ms:
            self.min_latency_ms = latency_ms
        if latency_ms > self.max_latency_ms:
            self.max_latency_ms = latency_ms

        for i, bucket in enumerate(LATENCY_BUCKETS_MS):
            if latency_ms <= bucket:
                self.bucket_counts[i] += 1
        if latency_ms > LATENCY_BUCKETS_MS[-1]:
            self.overflow_count += 1

        if self._summary is not None:
            self._summary.observe(latency_ms / 1000.0)

    def as_dict(self) -> dict[str, Any]:
        avg = self.total_latency_ms / self.total_observations if self.total_observations > 0 else 0.0
        return {
            "buckets": {f"le_{int(b)}ms": self.bucket_counts[i] for i, b in enumerate(LATENCY_BUCKETS_MS)},
            "overflow": self.overflow_count,
            "count": self.total_observations,
            "sum_ms": self.total_latency_ms,
            "avg_ms": avg,
            "min_ms": self.min_latency_ms if self.total_observations > 0 else 0.0,
            "max_ms": self.max_latency_ms,
        }


class McuVersion(msgspec.Struct):
    major: Annotated[int, msgspec.Meta(ge=0)]
    minor: Annotated[int, msgspec.Meta(ge=0)]


class SerialPipelineSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    inflight: dict[str, Any] | None = None
    last_completion: dict[str, Any] | None = None


class SerialLinkSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    connected: bool = False
    writer_attached: bool = False
    synchronised: bool = False


class HandshakeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    synchronised: bool = False
    attempts: Annotated[int, msgspec.Meta(ge=0)] = 0
    successes: Annotated[int, msgspec.Meta(ge=0)] = 0
    failures: Annotated[int, msgspec.Meta(ge=0)] = 0
    failure_streak: Annotated[int, msgspec.Meta(ge=0)] = 0
    last_error: str | None = None
    last_unix: float = 0.0
    last_duration: float = 0.0
    backoff_until: float = 0.0
    rate_limit_until: float = 0.0
    fatal_count: Annotated[int, msgspec.Meta(ge=0)] = 0
    fatal_reason: str | None = None
    fatal_detail: str | None = None
    fatal_unix: float = 0.0
    pending_nonce: bool = False
    nonce_length: Annotated[int, msgspec.Meta(ge=0)] = 0


class BridgeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    serial_link: SerialLinkSnapshot
    handshake: HandshakeSnapshot
    serial_pipeline: SerialPipelineSnapshot
    serial_flow: SerialFlowSnapshot
    mcu_version: McuVersion | None = None
    capabilities: dict[str, Any] | None = None


class SerialFlowSnapshot(msgspec.Struct):
    """Serial flow control statistics snapshot."""

    commands_sent: Annotated[int, msgspec.Meta(ge=0)]
    commands_acked: Annotated[int, msgspec.Meta(ge=0)]
    retries: Annotated[int, msgspec.Meta(ge=0)]
    failures: Annotated[int, msgspec.Meta(ge=0)]
    last_event_unix: float


class SerialFlowStats(BaseStats):
    """Serial flow control statistics (Mutable)."""

    commands_sent: int = 0
    commands_acked: int = 0
    retries: int = 0
    failures: int = 0
    last_event_unix: float = 0.0

    def as_snapshot(self) -> SerialFlowSnapshot:
        return SerialFlowSnapshot(
            commands_sent=self.commands_sent,
            commands_acked=self.commands_acked,
            retries=self.retries,
            failures=self.failures,
            last_event_unix=self.last_event_unix,
        )


class ProcessStats(msgspec.Struct):
    """Resource usage statistics for a single process."""

    name: str
    cpu_percent: float
    memory_rss_bytes: int


class BridgeStatus(msgspec.Struct, kw_only=True):
    """Root structure for the daemon status file."""

    # Serial Link
    serial_connected: bool
    serial_flow: SerialFlowSnapshot
    link_synchronised: bool
    handshake_attempts: Annotated[int, msgspec.Meta(ge=0)]
    handshake_successes: Annotated[int, msgspec.Meta(ge=0)]
    handshake_failures: Annotated[int, msgspec.Meta(ge=0)]
    handshake_last_error: str | None
    handshake_last_unix: float

    # MQTT
    mqtt_queue_size: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_queue_limit: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_messages_dropped: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_drop_counts: dict[str, int]

    # Spool
    mqtt_spooled_messages: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spooled_replayed: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spool_errors: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spool_degraded: bool
    mqtt_spool_failure_reason: str | None
    mqtt_spool_retry_attempts: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spool_backoff_until: float
    mqtt_spool_last_error: str | None
    mqtt_spool_recoveries: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spool_pending: Annotated[int, msgspec.Meta(ge=0)]

    # Storage
    file_storage_root: str
    file_storage_bytes_used: Annotated[int, msgspec.Meta(ge=0)]
    file_storage_quota_bytes: Annotated[int, msgspec.Meta(ge=0)]
    file_write_max_bytes: Annotated[int, msgspec.Meta(ge=0)]
    file_write_limit_rejections: Annotated[int, msgspec.Meta(ge=0)]
    file_storage_limit_rejections: Annotated[int, msgspec.Meta(ge=0)]

    # Queues
    datastore_keys: list[str]
    mailbox_size: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_bytes: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_dropped_messages: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_dropped_bytes: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_truncated_messages: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_truncated_bytes: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_incoming_dropped_messages: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_incoming_dropped_bytes: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_incoming_truncated_messages: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_incoming_truncated_bytes: Annotated[int, msgspec.Meta(ge=0)]
    console_queue_size: Annotated[int, msgspec.Meta(ge=0)]
    console_queue_bytes: Annotated[int, msgspec.Meta(ge=0)]
    console_dropped_chunks: Annotated[int, msgspec.Meta(ge=0)]
    console_dropped_bytes: Annotated[int, msgspec.Meta(ge=0)]
    console_truncated_chunks: Annotated[int, msgspec.Meta(ge=0)]
    console_truncated_bytes: Annotated[int, msgspec.Meta(ge=0)]

    # System
    mcu_paused: bool
    mcu_version: McuVersion | None
    watchdog_enabled: bool
    watchdog_interval: float
    watchdog_beats: Annotated[int, msgspec.Meta(ge=0)]
    watchdog_last_beat: float
    running_processes: list[str]
    allowed_commands: list[str]
    config_source: str
    process_stats: dict[str, ProcessStats] = msgspec.field(
        default_factory=lambda: cast(dict[str, ProcessStats], {})
    )

    # Snapshots
    bridge: BridgeSnapshot
    supervisors: dict[str, SupervisorSnapshot]
    heartbeat_unix: float
