"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Improved robustness for binary parsing (SIL-2) using Construct + Msgspec.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
from enum import IntEnum
from typing import Annotated, Any, ClassVar, Self, Type, TypeVar, cast

from binascii import crc32
import msgspec
from construct import (  # type: ignore
    Bytes,
    Check,
    Checksum,
    Construct,
    GreedyBytes,
    Int8ub,
    Int16ub,
    PascalString,
    Prefixed,  # type: ignore
    Switch,
    this,
    Struct as BinStruct,
)

from . import protocol

T = TypeVar("T", bound="BaseStruct")


class BaseStruct(msgspec.Struct, frozen=True):
    """Base class for hybrid Msgspec/Construct structures."""

    # Subclasses must define this schema
    _SCHEMA: ClassVar[Construct[Any]]

    @classmethod
    def decode(cls: Type[T], data: bytes | bytearray | memoryview) -> T:
        """Decode binary data into a typed Msgspec struct."""
        if not data:
            raise ValueError("Empty payload")

        # 1. Construct parses the binary data (validating lengths/structure)
        # Type checker complains about memoryview/bytearray, so explicit cast or strict bytes required
        # construct.parse usually accepts bytes-like objects, but pyright is strict.
        container: Any = cls._SCHEMA.parse(bytes(data))

        # 2. Msgspec creates the typed object (efficiently)
        # We filter the container to only include defined fields to avoid
        # passing internal construct metadata.
        return cls(**{k: v for k, v in container.items() if not k.startswith("_")})

    def encode(self) -> bytes:
        """Encode the typed Msgspec struct into binary data."""
        # msgspec.structs.asdict is highly optimized
        return self._SCHEMA.build(msgspec.structs.asdict(self))


# --- Binary Protocol Packets ---


class FileWritePacket(BaseStruct, frozen=True):
    path: str
    data: bytes

    _SCHEMA = BinStruct("path" / PascalString(Int8ub, "utf-8"), "data" / Prefixed(Int16ub, GreedyBytes))


class FileReadPacket(BaseStruct, frozen=True):
    path: str

    _SCHEMA = BinStruct("path" / PascalString(Int8ub, "utf-8"))


class FileRemovePacket(BaseStruct, frozen=True):
    path: str

    _SCHEMA = BinStruct("path" / PascalString(Int8ub, "utf-8"))


class VersionResponsePacket(BaseStruct, frozen=True):
    major: int
    minor: int

    _SCHEMA = BinStruct("major" / Int8ub, "minor" / Int8ub)


class FreeMemoryResponsePacket(BaseStruct, frozen=True):
    value: int

    _SCHEMA = BinStruct("value" / Int16ub)


class DigitalReadResponsePacket(BaseStruct, frozen=True):
    value: int

    _SCHEMA = BinStruct("value" / Int8ub)


class AnalogReadResponsePacket(BaseStruct, frozen=True):
    value: int

    _SCHEMA = BinStruct("value" / Int16ub)


class DatastoreGetPacket(BaseStruct, frozen=True):
    key: str

    _SCHEMA = BinStruct("key" / PascalString(Int8ub, "utf-8"))


class DatastorePutPacket(BaseStruct, frozen=True):
    key: str
    value: bytes

    _SCHEMA = BinStruct("key" / PascalString(Int8ub, "utf-8"), "value" / Prefixed(Int8ub, GreedyBytes))


class MailboxPushPacket(BaseStruct, frozen=True):
    data: bytes

    _SCHEMA = BinStruct("data" / Prefixed(Int16ub, GreedyBytes))


# --- Framing Schema ---

# [SIL-2] Construct Schema for Full Frame with Integrated CRC and Logic Checks
FRAME_STRUCT = BinStruct(
    "header" / BinStruct(
        "version" / Check(Int8ub == protocol.PROTOCOL_VERSION),
        "payload_len" / Check(Int16ub <= protocol.MAX_PAYLOAD_SIZE),
        "command_id" / protocol.CRC_COVERED_HEADER_STRUCT.command_id,
    ),
    "payload" / Bytes(this.header.payload_len),
    "crc" / Checksum(
        protocol.CRC_STRUCT,
        lambda data: (crc32(data) & 0xFFFFFFFF),
        this.header.version.build(protocol.PROTOCOL_VERSION) +
        this.header.payload_len.build(this.header.payload_len) +
        this.header.command_id.build(this.header.command_id) +
        this.payload
    ),
)

# [SIL-2] Dynamic Framing Schema using Switch for automatic payload resolution
DYNAMIC_FRAME_STRUCT = BinStruct(
    "header" / BinStruct(
        "version" / Check(Int8ub == protocol.PROTOCOL_VERSION),
        "payload_len" / Check(Int16ub <= protocol.MAX_PAYLOAD_SIZE),
        "command_id" / protocol.CRC_COVERED_HEADER_STRUCT.command_id,
    ),
    "payload" / Switch(this.header.command_id, {
        protocol.Command.CMD_FILE_WRITE: FileWritePacket._SCHEMA,
        protocol.Command.CMD_FILE_READ: FileReadPacket._SCHEMA,
        protocol.Command.CMD_FILE_REMOVE: FileRemovePacket._SCHEMA,
        protocol.Command.CMD_GET_VERSION_RESP: VersionResponsePacket._SCHEMA,
        protocol.Command.CMD_GET_FREE_MEMORY_RESP: FreeMemoryResponsePacket._SCHEMA,
        protocol.Command.CMD_DIGITAL_READ_RESP: DigitalReadResponsePacket._SCHEMA,
        protocol.Command.CMD_ANALOG_READ_RESP: AnalogReadResponsePacket._SCHEMA,
        protocol.Command.CMD_DATASTORE_GET: DatastoreGetPacket._SCHEMA,
        protocol.Command.CMD_DATASTORE_PUT: DatastorePutPacket._SCHEMA,
        protocol.Command.CMD_MAILBOX_PUSH: MailboxPushPacket._SCHEMA,
    }, default=Bytes(this.header.payload_len)),
    "crc" / Checksum(
        protocol.CRC_STRUCT,
        lambda data: (crc32(data) & 0xFFFFFFFF),
        this.header.version.build(protocol.PROTOCOL_VERSION) +
        this.header.payload_len.build(this.header.payload_len) +
        this.header.command_id.build(this.header.command_id) +
        this.payload
    ),
)


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


class SpoolRecord(msgspec.Struct, omit_defaults=True):
    """JSON-serializable record stored in the durable spool (RAM/Disk)."""

    topic_name: str
    payload: str
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: str | None = None
    user_properties: list[tuple[str, str]] = msgspec.field(default_factory=list)


UserProperty = tuple[str, str]


def _normalize_user_properties(raw: Any) -> tuple[UserProperty, ...]:
    if not (isinstance(raw, Iterable) and not isinstance(raw, (bytes, str))):
        return ()
    normalized: list[UserProperty] = []
    for entry in cast("Iterable[Any]", raw):
        if not (isinstance(entry, Iterable) and not isinstance(entry, (bytes, str))):
            continue
        entry_seq: list[Any] = list(cast("Iterable[Any]", entry))
        if len(entry_seq) < 2:
            continue
        normalized.append((str(entry_seq[0]), str(entry_seq[1])))
    return tuple(normalized)


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
    user_properties: tuple[UserProperty, ...] = ()

    def to_record(self) -> SpoolRecord:
        """Convert to a SpoolRecord struct for disk serialization."""
        # [SIL-2] Modernization: We transform binary fields to Base64 (JSON safe)
        # while keeping the rest declarative.
        payload_b64 = base64.b64encode(self.payload).decode("ascii")
        correlation_b64 = (
            base64.b64encode(self.correlation_data).decode("ascii") if self.correlation_data is not None else None
        )

        return SpoolRecord(
            topic_name=self.topic_name,
            payload=payload_b64,
            qos=int(self.qos),
            retain=self.retain,
            content_type=self.content_type,
            payload_format_indicator=self.payload_format_indicator,
            message_expiry_interval=self.message_expiry_interval,
            response_topic=self.response_topic,
            correlation_data=correlation_b64,
            user_properties=list(self.user_properties),
        )

    @classmethod
    def from_record(cls, record: SpoolRecord) -> Self:
        """Create a QueuedPublish instance from a SpoolRecord struct."""
        # [SIL-2] Coercion: Handle Base64 decoding manually, let msgspec handle the rest.
        payload = base64.b64decode(record.payload.encode("ascii"))
        correlation_data = (
            base64.b64decode(record.correlation_data.encode("ascii")) if record.correlation_data is not None else None
        )

        return cls(
            topic_name=record.topic_name,
            payload=payload,
            qos=record.qos,
            retain=record.retain,
            content_type=record.content_type,
            payload_format_indicator=record.payload_format_indicator,
            message_expiry_interval=record.message_expiry_interval,
            response_topic=record.response_topic,
            correlation_data=correlation_data,
            user_properties=tuple(record.user_properties),
        )


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


def _set_factory() -> set[int]:
    return set()


def _event_factory() -> asyncio.Event:
    return asyncio.Event()


class PendingCommand(msgspec.Struct):
    """Book-keeping for a tracked command in flight."""

    command_id: int
    expected_resp_ids: set[int] = msgspec.field(default_factory=_set_factory)
    completion: asyncio.Event = msgspec.field(default_factory=_event_factory)
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False

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


class SupervisorSnapshot(msgspec.Struct):
    restarts: Annotated[int, msgspec.Meta(ge=0)]
    last_failure_unix: float
    last_exception: str | None
    backoff_seconds: Annotated[float, msgspec.Meta(ge=0.0)]
    fatal: bool


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
    serial_flow: dict[str, Any]
    mcu_version: McuVersion | None = None
    capabilities: dict[str, Any] | None = None


class SerialFlowSnapshot(msgspec.Struct):
    """Serial flow control statistics snapshot."""

    commands_sent: Annotated[int, msgspec.Meta(ge=0)]
    commands_acked: Annotated[int, msgspec.Meta(ge=0)]
    retries: Annotated[int, msgspec.Meta(ge=0)]
    failures: Annotated[int, msgspec.Meta(ge=0)]
    last_event_unix: float


class BridgeStatus(msgspec.Struct):
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

    # Snapshots
    bridge: BridgeSnapshot
    supervisors: dict[str, SupervisorSnapshot]
    heartbeat_unix: float

