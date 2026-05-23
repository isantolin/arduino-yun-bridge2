"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Msgspec (SIL-2).
"""

from __future__ import annotations
from . import mcubridge_pb2 as pb

import asyncio
import enum
import functools
import re
import time
from collections.abc import Iterable, Mapping
from enum import IntEnum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    ClassVar,
    Final,
    TypeVar,
    cast,
)

import msgspec

PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"

# [SIL-2] Declarative bitmask definition for MCU capabilities.
# This ensures atomic bit-level parsing/building via 's C-backed engine.
# Order matches the protocol specification (bit 0 to bit 15).


class CapabilityFlag(enum.IntFlag):
    watchdog = 0x00000001
    rle = 0x00000002
    debug_frames = 0x00000004
    debug_io = 0x00000008
    eeprom = 0x00000010
    dac = 0x00000020
    hw_serial1 = 0x00000040
    fpu = 0x00000080
    logic_3v3 = 0x00000100
    big_buffer = 0x00000200
    i2c = 0x00000400
    spi = 0x00000800
    sd = 0x00001000


def _capabilities_to_int(feat_dict: dict[str, Any]) -> int:
    val = CapabilityFlag(0)
    for k, v in feat_dict.items():
        if v and hasattr(CapabilityFlag, k):
            val |= getattr(CapabilityFlag, k)
    return int(val)


def _int_to_capabilities(val: int) -> dict[str, bool]:
    flags = CapabilityFlag(val)
    return {k: bool(flags & getattr(CapabilityFlag, k)) for k in CapabilityFlag.__members__}


# [SIL-2] Compiled once at module load; reused across all AllowedCommandPolicy instances.
_TOKEN_SEP: Final = re.compile(r"[,\s]+")


class FlowEvent(str, enum.Enum):
    """Serial flow event types for typed dispatch."""

    SENT = "sent"
    ACK = "ack"
    RETRY = "retry"
    FAILURE = "failure"


@functools.lru_cache(maxsize=1)
def _get_action_lookup_map() -> dict[str, Any]:
    from .protocol import FileAction, ShellAction, SystemAction

    mapping: dict[str, Any] = {}
    for enum_cls in (FileAction, ShellAction, SystemAction):
        for e in enum_cls:
            mapping[e.value] = e
    return mapping


class TopicRoute(msgspec.Struct, frozen=True):
    """Parsed representation of an MQTT topic targeting the daemon."""

    raw: str
    prefix: str
    topic: Any  # Avoid circular import with .protocol.Topic
    segments: tuple[str, ...]

    @property
    def identifier(self) -> str:
        return self.segments[0] if self.segments else ""

    @property
    def action(self) -> Any:
        """Infer the service action from the first segment if applicable.
        Ignore segments that indicate a response flavor.
        """
        if not self.segments or "response" in self.segments or "value" in self.segments:
            return None
        val = self.segments[0]
        return _get_action_lookup_map().get(val, val)

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:] if len(self.segments) > 1 else ()


class RLEPayload(msgspec.Struct, frozen=True):
    """Encapsulates RLE-compressed data."""

    data: bytes

    def decode(self) -> bytes:
        """Decompress data using native RLE decoder."""
        from .rle import rle_decode

        if not self.data:
            return b""

        try:
            return rle_decode(self.data)
        except ValueError as exc:
            raise ValueError(f"RLE decompression failed: {exc}") from exc


# =============================================================================
# 2. Security and Policy Structures (msgspec)
# =============================================================================


class AllowedCommandPolicy(msgspec.Struct, frozen=True):
    """Normalised allow-list for shell/process commands."""

    entries: tuple[str, ...] = ()

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

    @classmethod
    def from_iterable(
        cls,
        entries: Iterable[str],
    ) -> AllowedCommandPolicy:
        """Return a deduplicated, lower-cased and sorted allow-list preserving wildcards."""
        all_tokens: list[str] = []
        for c in entries:
            if not c:
                continue
            tokens = _TOKEN_SEP.split(c.strip().lower())
            all_tokens.extend(t for t in tokens if t)

        items: set[str] = set(all_tokens)
        normalised = ("*",) if "*" in items else tuple(sorted(items))
        return cls(entries=normalised)


@functools.lru_cache(maxsize=1)
def _get_topic_auth_mapping() -> dict[tuple[str, str], str]:
    """Build and cache the (topic, action) → field-name map (deferred to avoid circular imports)."""
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

    return {
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
        """Build the optimized lookup cache using the module-level cached mapping."""
        mapping = _get_topic_auth_mapping()
        allowed = [k for k, attr in mapping.items() if getattr(self, attr)]
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
        DEFAULT_DEBUG,
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
    debug: bool = DEFAULT_DEBUG
    console_queue_limit_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PENDING_PIN_REQUESTS
    serial_retry_timeout: Annotated[float, msgspec.Meta(ge=0.01, le=30.0)] = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: Annotated[float, msgspec.Meta(ge=0.02, le=120.0)] = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_RETRY_LIMIT
    serial_fallback_threshold: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_FALLBACK_THRESHOLD
    serial_handshake_min_interval: Annotated[float, msgspec.Meta(ge=0.0, le=30.0)] = (
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    )
    serial_handshake_fatal_failures: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    mqtt_enabled: bool = True
    watchdog_enabled: bool = True
    watchdog_interval: Annotated[float, msgspec.Meta(ge=0.1, le=60.0)] = DEFAULT_WATCHDOG_INTERVAL
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

    def get_ssl_context(self) -> Any | None:
        """Create an ssl.SSLContext based on the current configuration (SIL-2)."""
        if not self.mqtt_tls:
            return None

        import ssl
        from mcubridge.config.const import MQTT_TLS_MIN_VERSION

        try:
            if self.mqtt_cafile:
                ca_path = Path(self.mqtt_cafile)
                if not ca_path.exists():
                    raise RuntimeError(f"MQTT TLS CA file missing: {self.mqtt_cafile}")
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
            else:
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

            context.minimum_version = MQTT_TLS_MIN_VERSION

            if self.mqtt_tls_insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            if self.mqtt_certfile or self.mqtt_keyfile:
                if not (self.mqtt_certfile and self.mqtt_keyfile):
                    raise ValueError("Both mqtt_certfile and mqtt_keyfile must be provided for mTLS.")
                context.load_cert_chain(self.mqtt_certfile, self.mqtt_keyfile)

            return context
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise RuntimeError(f"TLS setup failed: {exc}") from exc

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls

    def __post_init__(self) -> None:
        from mcubridge.config.const import (
            DEFAULT_SERIAL_SHARED_SECRET,
            VOLATILE_STORAGE_PATHS,
        )

        # [SIL-2] Semantic Policy Derivation
        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        self.allowed_commands = self.allowed_policy.entries if self.allowed_policy else ()

        if self.topic_authorization is None or isinstance(self.topic_authorization, dict):
            self.topic_authorization = (
                msgspec.convert(self.topic_authorization, TopicAuthorization)
                if self.topic_authorization
                else TopicAuthorization()
            )

        # [SIL-2] Strict Semantic Validations
        if not self.mqtt_topic or not any(filter(None, self.mqtt_topic.split("/"))):
            raise ValueError("mqtt_topic must contain at least one segment")

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
        if not self.allow_non_tmp_paths:
            if not any(self.mqtt_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                msg = f"FLASH PROTECTION: mqtt_spool_dir ({self.mqtt_spool_dir}) must be in a volatile location"
                raise ValueError(msg)

        if not self.allow_non_tmp_paths:
            if not any(self.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(
                    f"FLASH PROTECTION: file_system_root ({self.file_system_root}) must be in a volatile location"
                )


# =============================================================================
# 3. Operational Structures
# =============================================================================

T = TypeVar("T", bound="BaseStruct")


def _flatten_structured_value(
    key_prefix: str,
    value: Any,
    entries: list[pb.StructuredEntry],
) -> None:
    if isinstance(value, msgspec.Struct):
        struct_fields = msgspec.structs.asdict(value)
        for key, nested in struct_fields.items():
            _flatten_structured_value(f"{key_prefix}.{key}" if key_prefix else key, nested, entries)
        return
    if isinstance(value, Mapping):
        mapped_value = cast(Mapping[str, Any], value)
        for key, nested in mapped_value.items():
            key_name = str(key)
            _flatten_structured_value(f"{key_prefix}.{key_name}" if key_prefix else key_name, nested, entries)
        return

    entry = pb.StructuredEntry(key=key_prefix)
    if value is None:
        entry.null_value = True
    elif isinstance(value, bytes):
        entry.bytes_value = value
    elif isinstance(value, str):
        entry.string_value = value
    elif isinstance(value, bool):
        entry.bool_value = value
    elif isinstance(value, enum.IntEnum):
        entry.int_value = int(value)
    elif isinstance(value, int):
        entry.int_value = value
    elif isinstance(value, float):
        entry.float_value = value
    else:
        raise TypeError(f"Unsupported structured payload value for '{key_prefix}': {type(value)!r}")
    entries.append(entry)


def encode_structured_payload(payload: Mapping[str, Any] | msgspec.Struct) -> bytes:
    message = pb.StructuredPayload()
    source: Mapping[str, Any] = msgspec.structs.asdict(payload) if isinstance(payload, msgspec.Struct) else payload
    entries: list[pb.StructuredEntry] = []
    for key, value in source.items():
        _flatten_structured_value(str(key), value, entries)
    message.entries.extend(entries)
    return message.SerializeToString()


def _entry_value(entry: pb.StructuredEntry) -> Any:
    match entry.WhichOneof("value"):
        case "string_value":
            return entry.string_value
        case "bytes_value":
            return bytes(entry.bytes_value)
        case "bool_value":
            return entry.bool_value
        case "int_value":
            return entry.int_value
        case "float_value":
            return entry.float_value
        case "null_value":
            return None
        case _:
            raise ValueError(f"StructuredEntry '{entry.key}' missing value")


def decode_structured_payload(data: bytes) -> dict[str, Any]:
    message = pb.StructuredPayload()
    message.ParseFromString(data)
    decoded: dict[str, Any] = {}
    for entry in message.entries:
        cursor: dict[str, Any] = decoded
        parts = entry.key.split(".")
        for part in parts[:-1]:
            next_cursor_obj = cursor.get(part)
            if not isinstance(next_cursor_obj, dict):
                next_cursor: dict[str, Any] = {}
                cursor[part] = next_cursor
            else:
                next_cursor = cast(dict[str, Any], next_cursor_obj)
            cursor = next_cursor
        cursor[parts[-1]] = _entry_value(entry)
    return decoded


class BaseStruct(msgspec.Struct, frozen=True, array_like=True):
    """Base class for all serial payload packets.

    Encoded as protobuf payloads carried inside the framed RPC transport.
    """


# --- Binary Protocol Packets ---

# --- BEGIN GENERATED PACKETS --- DO NOT EDIT (auto-generated from spec.toml)


class VersionResponsePacket:
    PROTO_CLASS: Any = pb.VersionResponse
    _msg: Any
    
    major: Annotated[int, msgspec.Meta(ge=0)]
    
    minor: Annotated[int, msgspec.Meta(ge=0)]
    
    patch: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> VersionResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class FreeMemoryResponsePacket:
    PROTO_CLASS: Any = pb.FreeMemoryResponse
    _msg: Any
    
    value: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> FreeMemoryResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class PinModePacket:
    PROTO_CLASS: Any = pb.PinMode
    _msg: Any
    
    pin: Annotated[int, msgspec.Meta(ge=0)]
    
    mode: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> PinModePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class DigitalWritePacket:
    PROTO_CLASS: Any = pb.DigitalWrite
    _msg: Any
    
    pin: Annotated[int, msgspec.Meta(ge=0)]
    
    value: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> DigitalWritePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class AnalogWritePacket:
    PROTO_CLASS: Any = pb.AnalogWrite
    _msg: Any
    
    pin: Annotated[int, msgspec.Meta(ge=0)]
    
    value: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> AnalogWritePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class PinReadPacket:
    PROTO_CLASS: Any = pb.PinRead
    _msg: Any
    
    pin: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> PinReadPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class DigitalReadResponsePacket:
    PROTO_CLASS: Any = pb.DigitalReadResponse
    _msg: Any
    
    value: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> DigitalReadResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class AnalogReadResponsePacket:
    PROTO_CLASS: Any = pb.AnalogReadResponse
    _msg: Any
    
    value: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> AnalogReadResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class ConsoleWritePacket:
    PROTO_CLASS: Any = pb.ConsoleWrite
    _msg: Any
    
    data: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> ConsoleWritePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class DatastorePutPacket:
    PROTO_CLASS: Any = pb.DatastorePut
    _msg: Any
    
    key: str
    
    value: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> DatastorePutPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class DatastoreGetPacket:
    PROTO_CLASS: Any = pb.DatastoreGet
    _msg: Any
    
    key: str
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> DatastoreGetPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class DatastoreGetResponsePacket:
    PROTO_CLASS: Any = pb.DatastoreGetResponse
    _msg: Any
    
    value: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> DatastoreGetResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class MailboxPushPacket:
    PROTO_CLASS: Any = pb.MailboxPush
    _msg: Any
    
    data: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> MailboxPushPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class MailboxProcessedPacket:
    PROTO_CLASS: Any = pb.MailboxProcessed
    _msg: Any
    
    message_id: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> MailboxProcessedPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class MailboxAvailableResponsePacket:
    PROTO_CLASS: Any = pb.MailboxAvailableResponse
    _msg: Any
    
    count: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> MailboxAvailableResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class MailboxReadResponsePacket:
    PROTO_CLASS: Any = pb.MailboxReadResponse
    _msg: Any
    
    content: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> MailboxReadResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class FileWritePacket:
    PROTO_CLASS: Any = pb.FileWrite
    _msg: Any
    
    path: str
    
    data: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> FileWritePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class FileReadPacket:
    PROTO_CLASS: Any = pb.FileRead
    _msg: Any
    
    path: str
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> FileReadPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class FileRemovePacket:
    PROTO_CLASS: Any = pb.FileRemove
    _msg: Any
    
    path: str
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> FileRemovePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class FileReadResponsePacket:
    PROTO_CLASS: Any = pb.FileReadResponse
    _msg: Any
    
    content: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> FileReadResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class ProcessRunAsyncPacket:
    PROTO_CLASS: Any = pb.ProcessRunAsync
    _msg: Any
    
    command: str
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> ProcessRunAsyncPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class ProcessRunAsyncResponsePacket:
    PROTO_CLASS: Any = pb.ProcessRunAsyncResponse
    _msg: Any
    
    pid: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> ProcessRunAsyncResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class ProcessPollPacket:
    PROTO_CLASS: Any = pb.ProcessPoll
    _msg: Any
    
    pid: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> ProcessPollPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class ProcessPollResponsePacket:
    PROTO_CLASS: Any = pb.ProcessPollResponse
    _msg: Any
    
    status: Annotated[int, msgspec.Meta(ge=0)]
    
    exit_code: Annotated[int, msgspec.Meta(ge=0)]
    
    stdout_data: bytes
    
    stderr_data: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> ProcessPollResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class ProcessKillPacket:
    PROTO_CLASS: Any = pb.ProcessKill
    _msg: Any
    
    pid: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> ProcessKillPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class AckPacket:
    PROTO_CLASS: Any = pb.AckPacket
    _msg: Any
    
    command_id: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> AckPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class HandshakeConfigPacket:
    PROTO_CLASS: Any = pb.HandshakeConfig
    _msg: Any
    
    ack_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]
    
    ack_retry_limit: Annotated[int, msgspec.Meta(ge=0)]
    
    response_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> HandshakeConfigPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class SetBaudratePacket:
    PROTO_CLASS: Any = pb.SetBaudratePacket
    _msg: Any
    
    baudrate: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> SetBaudratePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class LinkSyncPacket:
    PROTO_CLASS: Any = pb.LinkSync
    _msg: Any
    
    nonce: bytes
    
    tag: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> LinkSyncPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class EnterBootloaderPacket:
    PROTO_CLASS: Any = pb.EnterBootloader
    _msg: Any
    
    magic: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> EnterBootloaderPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class SpiTransferPacket:
    PROTO_CLASS: Any = pb.SpiTransfer
    _msg: Any
    
    data: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> SpiTransferPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class SpiTransferResponsePacket:
    PROTO_CLASS: Any = pb.SpiTransferResponse
    _msg: Any
    
    data: bytes
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> SpiTransferResponsePacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


class SpiConfigPacket:
    PROTO_CLASS: Any = pb.SpiConfig
    _msg: Any
    
    bit_order: Annotated[int, msgspec.Meta(ge=0)]
    
    data_mode: Annotated[int, msgspec.Meta(ge=0)]
    
    frequency: Annotated[int, msgspec.Meta(ge=0)]
    

    def __init__(self, **kwargs: Any) -> None:
        self._msg = self.PROTO_CLASS(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._msg, name)

    @classmethod
    def decode(cls, data: bytes) -> SpiConfigPacket:
        msg = cls.PROTO_CLASS()
        msg.ParseFromString(data)
        instance = cls()
        instance._msg = msg
        return instance

    def encode(self) -> bytes:
        return self._msg.SerializeToString() # type: ignore


# --- END GENERATED PACKETS ---


class GenericResponsePacket(msgspec.Struct, frozen=True):
    """Generic high-level API response packet."""

    status: str
    message: str | None = None
    data: dict[str, Any] | None = None


# --- Manual Packet Classes (require special handling) ---


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
    feat_mask: int

    @property
    def features(self) -> CapabilitiesFeatures:
        """Expand bitmask into structured features object."""
        feat_dict = _int_to_capabilities(self.feat_mask)
        return msgspec.convert(feat_dict, CapabilitiesFeatures)

    @classmethod
    def from_parts(cls, ver: int, arch: int, dig: int, ana: int, features: CapabilitiesFeatures) -> CapabilitiesPacket:
        """Factory to create packet from expanded features."""
        mask = _capabilities_to_int(msgspec.structs.asdict(features))
        return cls(ver=ver, arch=arch, dig=dig, ana=ana, feat_mask=mask)

    @classmethod
    def decode(cls, data: bytes) -> CapabilitiesPacket:
        msg = pb.Capabilities()
        msg.ParseFromString(data)
        return cls(
            ver=msg.ver,
            arch=msg.arch,
            dig=msg.dig,
            ana=msg.ana,
            feat_mask=msg.feat,
        )

    def encode(self) -> bytes:
        msg = pb.Capabilities(
            ver=self.ver,
            arch=self.arch,
            dig=self.dig,
            ana=self.ana,
            feat=self.feat_mask,
        )
        return msg.SerializeToString()


# [SIL-2] Payload Schema Map: Centralized registry for all command payloads.
# This eliminates manual if/elif dispatching across components.

# --- Operational Constants ---

MAX_COMMAND_LEN: Final[int] = 512


class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- High-Level Structure (Msgspec Only) ---


class ShellCommandPayload(msgspec.Struct, frozen=True):
    """Represents a shell command request coming from MQTT.

    Accepts either plain text or protobuf ProcessRunAsync payloads.
    """

    command: Annotated[str, msgspec.Meta(min_length=1, max_length=MAX_COMMAND_LEN)]


class ShellPidPayload(msgspec.Struct, frozen=True):
    """MQTT payload specifying an async shell PID to operate on."""

    pid: Annotated[int, msgspec.Meta(gt=0, le=65535)]  # UINT16_MAX


class SerialTimingWindow(msgspec.Struct, frozen=True):
    """Derived serial retry/response windows used by both MCU and MPU."""

    ack_timeout_ms: Annotated[int, msgspec.Meta(ge=10, le=50000)]
    response_timeout_ms: Annotated[int, msgspec.Meta(ge=100, le=50000)]
    retry_limit: Annotated[int, msgspec.Meta(ge=1, le=100)]

    @property
    def ack_timeout_seconds(self) -> float:
        return self.ack_timeout_ms / 1000.0

    @property
    def response_timeout_seconds(self) -> float:
        return self.response_timeout_ms / 1000.0


class MqttPayload(msgspec.Struct, frozen=True):
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False
    properties: dict[str, Any] = {}


class PinRequest(msgspec.Struct, frozen=True):
    pin: int
    state: str


class PendingPinRequest(msgspec.Struct):
    """Pending pin read request."""

    pin: int
    reply_context: Any | None = None  # Message | None


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


class QueuedPublish(msgspec.Struct, frozen=True):
    """Serializable MQTT publish packet used by the durable spool."""

    topic_name: str
    payload: bytes
    qos: Annotated[int, msgspec.Meta(ge=0, le=2)] = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    response_payload: bytes | None = None
    user_properties: tuple[UserProperty, ...] = ()
    subscription_identifier: tuple[int, ...] | None = None


# --- Process Service Structures ---


class ProcessOutputBatch(msgspec.Struct):
    """Structured payload describing PROCESS_POLL results."""

    status_byte: Annotated[int, msgspec.Meta(ge=0, le=255)]
    exit_code: Annotated[int, msgspec.Meta(ge=0, le=255)]
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool


# --- Serial Flow Structures ---


class PendingCommand(msgspec.Struct):
    """Book-keeping for a tracked command in flight."""

    command_id: int
    expected_resp_ids: set[int] = msgspec.field(default_factory=lambda: cast(set[int], set()))
    completion: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False
    reply_topic: str | None = None
    correlation_data: bytes | None = None
    response_payload: bytes | None = None

    def mark_success(self, payload: bytes | None = None) -> None:
        self.response_payload = payload
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        if not self.completion.is_set():
            self.completion.set()


# --- Status Structures ---


_SnapshotT = TypeVar("_SnapshotT", bound=msgspec.Struct)


class BaseStats(msgspec.Struct):
    """Base for statistics containers providing standard dict conversion.

    Subclasses that define ``SNAPSHOT_TYPE`` get a generic ``as_snapshot()``
    that converts all fields into the frozen snapshot class via msgspec.
    """

    SNAPSHOT_TYPE: ClassVar[type | None] = None

    def as_snapshot(self) -> msgspec.Struct:
        """Convert mutable stats to a frozen snapshot struct."""
        snap_cls = self.__class__.SNAPSHOT_TYPE
        if snap_cls is None:
            raise NotImplementedError(f"{self.__class__.__name__} has no SNAPSHOT_TYPE")
        return cast(msgspec.Struct, msgspec.convert(msgspec.structs.asdict(self), snap_cls))


class SupervisorSnapshot(msgspec.Struct):
    restarts: Annotated[int, msgspec.Meta(ge=0)]
    last_failure_unix: float
    last_exception: str | None
    backoff_seconds: Annotated[float, msgspec.Meta(ge=0.0)]
    fatal: bool


class SupervisorStats(BaseStats):
    """Task supervisor statistics."""

    SNAPSHOT_TYPE: ClassVar[type | None] = SupervisorSnapshot

    restarts: int = 0
    last_failure_unix: float = 0.0
    last_exception: str | None = None
    backoff_seconds: float = 0.0
    fatal: bool = False

    def as_snapshot(self) -> SupervisorSnapshot:
        return cast(SupervisorSnapshot, super().as_snapshot())


class McuCapabilities(msgspec.Struct):
    """Hardware capabilities reported by the MCU."""

    protocol_version: int = 0
    board_arch: int = 0
    num_digital_pins: int = 0
    num_analog_inputs: int = 0
    features: CapabilitiesFeatures | None = None

    @property
    def arch_name(self) -> str:
        from .protocol import ARCHITECTURE_DISPLAY_NAMES

        return ARCHITECTURE_DISPLAY_NAMES.get(self.board_arch, f"Unknown (0x{self.board_arch:02X})")


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


class PipelineEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Immutable snapshot of a single serial pipeline RPC event (SIL-2)."""

    event: str
    command_id: int
    attempt: int
    ack_received: bool
    status: int | None
    timestamp: float


class McuVersion(msgspec.Struct):
    major: Annotated[int, msgspec.Meta(ge=0)]
    minor: Annotated[int, msgspec.Meta(ge=0)]
    patch: Annotated[int, msgspec.Meta(ge=0)] = 0


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
    last_unix: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
    last_duration: float = 0.0
    backoff_until: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
    rate_limit_until: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
    fatal_count: Annotated[int, msgspec.Meta(ge=0)] = 0
    fatal_reason: str | None = None
    fatal_detail: str | None = None
    fatal_unix: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
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

    SNAPSHOT_TYPE: ClassVar[type | None] = SerialFlowSnapshot

    commands_sent: int = 0
    commands_acked: int = 0
    retries: int = 0
    failures: int = 0
    last_event_unix: float = 0.0

    def as_snapshot(self) -> SerialFlowSnapshot:
        return cast(SerialFlowSnapshot, super().as_snapshot())


class ProcessStats(msgspec.Struct):
    """Resource usage statistics for a single process."""

    name: str
    cpu_percent: Annotated[float, msgspec.Meta(ge=0.0)]
    memory_rss_bytes: Annotated[int, msgspec.Meta(ge=0)]
