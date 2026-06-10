"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Msgspec (SIL-2).
"""

from __future__ import annotations
from google.protobuf.message import Message as ProtobufMessage
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
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"

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
    """Per-topic allow flags for MQTT-driven actions."""

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

    _allowed_cache: Final[frozenset[tuple[str, str]]] = frozenset()

    def __post_init__(self) -> None:
        mapping = _get_topic_auth_mapping()
        allowed = [k for k, attr in mapping.items() if getattr(self, attr)]
        object.__setattr__(self, "_allowed_cache", frozenset(allowed))

    def allows(self, topic: str, action: str) -> bool:
        return (topic.lower(), action.lower()) in self._allowed_cache


class RuntimeConfig(msgspec.Struct, kw_only=True):
    """Strongly typed configuration for the daemon."""

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
        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        self.allowed_commands = self.allowed_policy.entries if self.allowed_policy else ()
        if self.topic_authorization is None or isinstance(self.topic_authorization, dict):
            self.topic_authorization = (
                msgspec.convert(self.topic_authorization, TopicAuthorization)
                if self.topic_authorization
                else TopicAuthorization()
            )
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
        if isinstance(self.serial_shared_secret, bytes):
            unique_symbols = {byte for byte in self.serial_shared_secret}
            if len(unique_symbols) < 4 and self.serial_shared_secret != DEFAULT_SERIAL_SHARED_SECRET:
                raise ValueError("serial_shared_secret must contain at least four distinct bytes")
        if self.file_storage_quota_bytes < self.file_write_max_bytes:
            raise ValueError("file_storage_quota_bytes must be greater than or equal to file_write_max_bytes")
        if self.mailbox_queue_bytes_limit < self.mailbox_queue_limit:
            raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")
        if not self.allow_non_tmp_paths:
            if not any(self.mqtt_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(f"FLASH PROTECTION: mqtt_spool_dir ({self.mqtt_spool_dir}) must be in a volatile location")
            if not any(self.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(f"FLASH PROTECTION: file_system_root ({self.file_system_root}) must be in a volatile location")


def encode_structured_payload(payload: Mapping[str, Any] | msgspec.Struct) -> bytes:
    """[DEPRECATED] Use specific Protobuf messages directly."""
    message = pb.StructuredPayload()
    source: Mapping[str, Any] = msgspec.structs.asdict(payload) if isinstance(payload, msgspec.Struct) else payload
    entries: list[pb.StructuredEntry] = []
    for key, value in source.items():
        _flatten_structured_value(str(key), value, entries)
    message.entries.extend(entries)
    return message.SerializeToString()


def _flatten_structured_value(key_prefix: str, value: Any, entries: list[pb.StructuredEntry]) -> None:
    if isinstance(value, msgspec.Struct):
        struct_fields = msgspec.structs.asdict(value)
        for key, nested in struct_fields.items():
            _flatten_structured_value(f"{key_prefix}.{key}" if key_prefix else key, nested, entries)
        return
    if isinstance(value, (list, tuple)):
        for i, nested in enumerate(value):
            _flatten_structured_value(f"{key_prefix}.{i}" if key_prefix else str(i), nested, entries)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _flatten_structured_value(f"{key_prefix}.{str(key)}" if key_prefix else str(key), nested, entries)
        return
    entry = pb.StructuredEntry(key=key_prefix)
    if value is None: entry.null_value = True
    elif isinstance(value, bytes): entry.bytes_value = value
    elif isinstance(value, str): entry.string_value = value
    elif isinstance(value, bool): entry.bool_value = value
    elif isinstance(value, (int, enum.IntEnum)): entry.int_value = int(value)
    elif isinstance(value, float): entry.float_value = value
    else: raise TypeError(f"Unsupported structured payload value for '{key_prefix}': {type(value)!r}")
    entries.append(entry)


class PendingPinRequest(msgspec.Struct):
    pin: int
    reply_context: Any | None = None


class QOSLevel(IntEnum):
    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


UserProperty = tuple[str, str]


def build_mqtt_properties(message: QueuedPublish) -> Properties:
    props = Properties(PacketTypes.PUBLISH)
    _MAP = {"content_type": "ContentType", "payload_format_indicator": "PayloadFormatIndicator",
            "message_expiry_interval": "MessageExpiryInterval", "response_topic": "ResponseTopic",
            "correlation_data": "CorrelationData", "user_properties": "UserProperty", "topic_alias": "TopicAlias"}
    for field, paho_name in _MAP.items():
        val = getattr(message, field)
        if val is not None:
            setattr(props, paho_name, list(val) if field == "user_properties" else val)
    if message.subscription_identifier is not None:
        props.SubscriptionIdentifier = list(message.subscription_identifier)
    return props


class QueuedPublish(msgspec.Struct, frozen=True):
    topic_name: str
    payload: bytes
    qos: Annotated[int, msgspec.Meta(ge=0, le=2)] = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: tuple[UserProperty, ...] = ()
    subscription_identifier: tuple[int, ...] | None = None
    topic_alias: int | None = None


class ProcessOutputBatch(msgspec.Struct):
    status_byte: Annotated[int, msgspec.Meta(ge=0, le=255)]
    exit_code: Annotated[int, msgspec.Meta(ge=0, le=255)]
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool


class PendingCommand(msgspec.Struct):
    command_id: int
    expected_resp_ids: set[int] = msgspec.field(default_factory=lambda: cast(set[int], set()))
    completion: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False
    reply_topic: str | None = None
    correlation_data: bytes | None = None
    response_payload: bytes | ProtobufMessage | None = None

    def mark_success(self, payload: bytes | ProtobufMessage | None = None) -> None:
        self.response_payload = payload
        self.success = True
        if not self.completion.is_set(): self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        if not self.completion.is_set(): self.completion.set()


class BaseStats(msgspec.Struct):
    def as_snapshot(self) -> ProtobufMessage:
        raise NotImplementedError()


class SupervisorStats(BaseStats):
    restarts: int = 0
    last_failure_unix: float = 0.0
    last_exception: str | None = None
    backoff_seconds: float = 0.0
    fatal: bool = False

    def as_snapshot(self) -> pb.SupervisorSnapshot:
        return pb.SupervisorSnapshot(
            restarts=self.restarts,
            last_failure_unix=self.last_failure_unix,
            last_exception=self.last_exception or "",
            backoff_seconds=self.backoff_seconds,
            fatal=self.fatal,
        )


class SerialThroughputStats(BaseStats):
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

    def as_snapshot(self) -> pb.SerialThroughputSnapshot:
        return pb.SerialThroughputSnapshot(
            bytes_sent=self.bytes_sent,
            bytes_received=self.bytes_received,
            frames_sent=self.frames_sent,
            frames_received=self.frames_received,
            last_tx_unix=self.last_tx_unix,
            last_rx_unix=self.last_rx_unix,
        )


class SerialFlowStats(BaseStats):
    commands_sent: int = 0
    commands_acked: int = 0
    retries: int = 0
    failures: int = 0
    last_event_unix: float = 0.0

    def as_snapshot(self) -> pb.SerialFlowSnapshot:
        return pb.SerialFlowSnapshot(
            commands_sent=self.commands_sent,
            commands_acked=self.commands_acked,
            retries=self.retries,
            failures=self.failures,
            last_event_unix=self.last_event_unix,
        )


class ProcessStats(msgspec.Struct):
    name: str
    cpu_percent: Annotated[float, msgspec.Meta(ge=0.0)]
    memory_rss_bytes: Annotated[int, msgspec.Meta(ge=0)]

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
