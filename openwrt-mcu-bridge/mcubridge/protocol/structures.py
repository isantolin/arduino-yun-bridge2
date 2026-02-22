"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Improved robustness for binary parsing (SIL-2) using Construct + Msgspec.
"""

from __future__ import annotations

import asyncio
import time
from binascii import crc32
from collections.abc import Iterable
from enum import IntEnum
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, Self, Type, TypeVar, cast

import msgspec
import construct as construct_raw
from . import protocol

if TYPE_CHECKING:
    construct: Any = construct_raw
    BinStruct = construct.Struct
    Construct = construct_raw.Construct[Any]
else:
    construct = construct_raw
    BinStruct = construct.Struct
    Construct = construct_raw.Construct

# --- Basic Binary Types (Restored from protocol.py) ---
UINT8_STRUCT: Final = construct.Int8ub
UINT16_STRUCT: Final = construct.Int16ub
UINT32_STRUCT: Final = construct.Int32ub
NONCE_COUNTER_STRUCT: Final = construct.Int64ub
CRC_STRUCT: Final = construct.Int32ub

# [SIL-2] Explicit Command ID Structure
# Separates compression flag from the command identifier for explicit handling.
_RawCommandIdStruct: Final = construct.BitStruct(
    "compressed" / construct.Flag,
    "id" / construct.Enum(
        construct.BitsInteger(15),
        protocol.Command,
        protocol.Status,
        _default=construct.Pass
    ),
)


class CommandIdAdapter(construct.Adapter):
    """Transparently converts between integer command ID (with flag) and BitStruct."""

    def _decode(self, obj: Any, context: Any, path: Any) -> int:
        # Convert Enum/int from BitStruct back to combined integer
        cmd_val = int(obj.id)
        if obj.compressed:
            cmd_val |= protocol.CMD_FLAG_COMPRESSED
        return cmd_val

    def _encode(self, obj: int, context: Any, path: Any) -> dict[str, Any]:
        # Split combined integer into Flag + ID for BitStruct
        return {
            "compressed": bool(obj & protocol.CMD_FLAG_COMPRESSED),
            "id": obj & ~protocol.CMD_FLAG_COMPRESSED,
        }


CommandIdStruct: Final = CommandIdAdapter(_RawCommandIdStruct)

CRC_COVERED_HEADER_STRUCT: Final = BinStruct(
    "version" / construct.Int8ub,
    "payload_len" / construct.Int16ub,
    "command_id" / CommandIdStruct,
)

T = TypeVar("T", bound="BaseStruct")


class BaseStruct(msgspec.Struct, frozen=True):
    """Base class for hybrid Msgspec/Construct structures."""

    # Subclasses must define this schema
    _SCHEMA: ClassVar[Construct]

    @classmethod
    def decode(cls: Type[T], data: bytes | bytearray | memoryview) -> T:
        """Decode binary data into a typed Msgspec struct."""
        if not data:
            raise ValueError("Empty payload")

        # 1. Construct parses the binary data (validating lengths/structure)
        container: Any = cls._SCHEMA.parse(bytes(data))

        # 2. Msgspec creates the typed object (efficiently)
        return msgspec.convert(container, cls)

    def encode(self) -> bytes:
        """Encode the typed Msgspec struct into binary data."""
        return self._SCHEMA.build(msgspec.structs.asdict(self))


# --- Binary Protocol Packets ---


class FileWritePacket(BaseStruct, frozen=True):
    path: str
    data: bytes

    _SCHEMA = BinStruct(
        "path" / construct.PascalString(construct.Int8ub, "utf-8"),
        "data" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes),
    )


class FileReadPacket(BaseStruct, frozen=True):
    path: str

    _SCHEMA = BinStruct("path" / construct.PascalString(construct.Int8ub, "utf-8"))


class FileReadResponsePacket(BaseStruct, frozen=True):
    content: bytes

    _SCHEMA = BinStruct("content" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes))


class FileRemovePacket(BaseStruct, frozen=True):
    path: str

    _SCHEMA = BinStruct("path" / construct.PascalString(construct.Int8ub, "utf-8"))


class VersionResponsePacket(BaseStruct, frozen=True):
    major: Annotated[int, msgspec.Meta(ge=0)]
    minor: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("major" / construct.Int8ub, "minor" / construct.Int8ub)


class FreeMemoryResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("value" / construct.Int16ub)


class DigitalReadResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("value" / construct.Int8ub)


class AnalogReadResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("value" / construct.Int16ub)


class DatastoreGetPacket(BaseStruct, frozen=True):
    key: str

    _SCHEMA = BinStruct("key" / construct.PascalString(construct.Int8ub, "utf-8"))


class DatastoreGetResponsePacket(BaseStruct, frozen=True):
    value: bytes

    _SCHEMA = BinStruct("value" / construct.Prefixed(construct.Int8ub, construct.GreedyBytes))


class DatastorePutPacket(BaseStruct, frozen=True):
    key: str
    value: bytes

    _SCHEMA = BinStruct(
        "key" / construct.PascalString(construct.Int8ub, "utf-8"),
        "value" / construct.Prefixed(construct.Int8ub, construct.GreedyBytes),
    )


class MailboxPushPacket(BaseStruct, frozen=True):
    data: bytes

    _SCHEMA = BinStruct("data" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes))


class MailboxProcessedPacket(BaseStruct, frozen=True):
    message_id: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("message_id" / construct.Int16ub)


class MailboxAvailableResponsePacket(BaseStruct, frozen=True):
    count: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("count" / construct.Int16ub)


class MailboxReadResponsePacket(BaseStruct, frozen=True):
    content: bytes

    _SCHEMA = BinStruct("content" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes))


class PinModePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    mode: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("pin" / construct.Int8ub, "mode" / construct.Int8ub)


class DigitalWritePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("pin" / construct.Int8ub, "value" / construct.Int8ub)


class AnalogWritePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("pin" / construct.Int8ub, "value" / construct.Int8ub)


class PinReadPacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("pin" / construct.Int8ub)


class AckPacket(BaseStruct, frozen=True):
    command_id: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("command_id" / construct.Int16ub)


class ConsoleWritePacket(BaseStruct, frozen=True):
    data: bytes

    _SCHEMA = BinStruct("data" / construct.GreedyBytes)


class ProcessRunPacket(BaseStruct, frozen=True):
    command: str

    _SCHEMA = BinStruct("command" / construct.GreedyString("utf-8"))


class ProcessRunAsyncPacket(BaseStruct, frozen=True):
    command: str

    _SCHEMA = BinStruct("command" / construct.GreedyString("utf-8"))


class ProcessKillPacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("pid" / construct.Int16ub)


class ProcessPollPacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("pid" / construct.Int16ub)


class ProcessRunResponsePacket(BaseStruct, frozen=True):
    status: Annotated[int, msgspec.Meta(ge=0)]
    stdout: bytes
    stderr: bytes
    exit_code: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct(
        "status" / construct.Int8ub,
        "stdout" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes),
        "stderr" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes),
        "exit_code" / construct.Int8ub,
    )


class ProcessRunAsyncResponsePacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("pid" / construct.Int16ub)


class ProcessPollResponsePacket(BaseStruct, frozen=True):
    status: Annotated[int, msgspec.Meta(ge=0)]
    exit_code: Annotated[int, msgspec.Meta(ge=0)]
    stdout: bytes
    stderr: bytes

    _SCHEMA = BinStruct(
        "status" / construct.Int8ub,
        "exit_code" / construct.Int8ub,
        "stdout" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes),
        "stderr" / construct.Prefixed(construct.Int16ub, construct.GreedyBytes),
    )


class HandshakeConfigPacket(BaseStruct, frozen=True):
    ack_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]
    ack_retry_limit: Annotated[int, msgspec.Meta(ge=0)]
    response_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct(
        "ack_timeout_ms" / construct.Int16ub,
        "ack_retry_limit" / construct.Int8ub,
        "response_timeout_ms" / construct.Int32ub,
    )


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
    large_buffer: bool
    i2c: bool


class CapabilitiesPacket(BaseStruct, frozen=True):
    ver: Annotated[int, msgspec.Meta(ge=0)]
    arch: Annotated[int, msgspec.Meta(ge=0)]
    dig: Annotated[int, msgspec.Meta(ge=0)]
    ana: Annotated[int, msgspec.Meta(ge=0)]
    feat: CapabilitiesFeatures

    _SCHEMA = BinStruct(
        "ver" / construct.Int8ub,
        "arch" / construct.Int8ub,
        "dig" / construct.Int8ub,
        "ana" / construct.Int8ub,
        "feat" / construct.BitStruct(
            construct.Padding(32 - 11),
            "i2c" / construct.Flag,
            "large_buffer" / construct.Flag,
            "logic_3v3" / construct.Flag,
            "fpu" / construct.Flag,
            "hw_serial1" / construct.Flag,
            "dac" / construct.Flag,
            "eeprom" / construct.Flag,
            "debug_io" / construct.Flag,
            "debug_frames" / construct.Flag,
            "rle" / construct.Flag,
            "watchdog" / construct.Flag,
        )
    )


class SetBaudratePacket(BaseStruct, frozen=True):
    baudrate: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct("baudrate" / construct.Int32ub)


# --- Framing Schema ---

def _compute_crc32(data: bytes) -> int:
    return crc32(data)


# [SIL-2] Integrated Framing Schema
# Uses Checksum for automatic CRC32 validation/generation.
# Uses Switch for automatic payload schema selection.
# Uses RawCopy to allow access to raw bytes for Checksum and legacy byte-based handlers.
FRAME_STRUCT = BinStruct(
    "content" / construct.RawCopy(BinStruct(
        "header" / CRC_COVERED_HEADER_STRUCT,
        "payload" / construct.RawCopy(construct.IfThenElse(
            (construct.this.header.command_id & protocol.CMD_FLAG_COMPRESSED),
            # If compressed, do not parse payload schema (it's raw compressed bytes)
            construct.Bytes(construct.this.header.payload_len),
            # If not compressed, select schema based on ID, strictly bounded by payload_len
            construct.FixedSized(construct.this.header.payload_len, construct.Switch((construct.this.header.command_id & ~protocol.CMD_FLAG_COMPRESSED), {
                protocol.Command.CMD_FILE_WRITE: FileWritePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_FILE_READ: FileReadPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_FILE_REMOVE: FileRemovePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_GET_VERSION_RESP: VersionResponsePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_GET_FREE_MEMORY_RESP: FreeMemoryResponsePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_DIGITAL_READ_RESP: DigitalReadResponsePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_ANALOG_READ_RESP: AnalogReadResponsePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_DATASTORE_GET: DatastoreGetPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_DATASTORE_PUT: DatastorePutPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_MAILBOX_PUSH: MailboxPushPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_SET_PIN_MODE: PinModePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_DIGITAL_WRITE: DigitalWritePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_ANALOG_WRITE: AnalogWritePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_DIGITAL_READ: PinReadPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_ANALOG_READ: PinReadPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_CONSOLE_WRITE: ConsoleWritePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_RUN: ProcessRunPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_RUN_ASYNC: ProcessRunAsyncPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_POLL: ProcessPollPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_KILL: ProcessKillPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_RUN_RESP: ProcessRunResponsePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_RUN_ASYNC_RESP: ProcessRunAsyncResponsePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_POLL_RESP: ProcessPollResponsePacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
                protocol.Command.CMD_LINK_RESET: HandshakeConfigPacket._SCHEMA,  # pyright: ignore[reportPrivateUsage]
            }, default=construct.GreedyBytes))
        )),
    )),
    "crc" / construct.Checksum(
        CRC_STRUCT,
        _compute_crc32,
        construct.this.content.data
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
    payload: bytes
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: list[tuple[str, str]] = msgspec.field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


UserProperty = tuple[str, str]


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
            user_properties=list(self.user_properties),
        )

    @classmethod
    def from_record(cls, record: SpoolRecord | dict[str, Any]) -> Self:
        """Create a QueuedPublish instance from a SpoolRecord struct or dict."""
        data: dict[str, Any] = record if isinstance(record, dict) else msgspec.structs.asdict(record)

        payload = data.get("payload", b"")
        if isinstance(payload, str):
             payload = payload.encode("utf-8") # Fallback

        correlation_data = data.get("correlation_data")
        if isinstance(correlation_data, str):
             correlation_data = correlation_data.encode("utf-8") # Fallback

        raw_props = data.get("user_properties", ())
        user_properties: list[tuple[str, str]] = []  # pyright: ignore[reportUnknownVariableType]
        if isinstance(raw_props, Iterable):
            for item in cast("Iterable[Any]", raw_props):
                if isinstance(item, (list, tuple)) and len(item) >= 2:  # pyright: ignore[reportUnknownArgumentType]
                    k = str(item[0])  # pyright: ignore[reportUnknownArgumentType]
                    v = str(item[1])  # pyright: ignore[reportUnknownArgumentType]
                    user_properties.append((k, v))

        return cls(
            topic_name=str(data.get("topic_name", "")),
            payload=payload,
            qos=int(data.get("qos", 0)),
            retain=bool(data.get("retain", False)),
            content_type=data.get("content_type"),
            payload_format_indicator=data.get("payload_format_indicator"),
            message_expiry_interval=data.get("message_expiry_interval"),
            response_topic=data.get("response_topic"),
            correlation_data=correlation_data,
            user_properties=tuple(user_properties),  # pyright: ignore[reportUnknownArgumentType]
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


class SupervisorSnapshot(msgspec.Struct):
    restarts: Annotated[int, msgspec.Meta(ge=0)]
    last_failure_unix: float
    last_exception: str | None
    backoff_seconds: Annotated[float, msgspec.Meta(ge=0.0)]
    fatal: bool


class SupervisorStats(msgspec.Struct):
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


class McuCapabilities(msgspec.Struct):
    """Hardware capabilities reported by the MCU."""

    protocol_version: int = 0
    board_arch: int = 0
    num_digital_pins: int = 0
    num_analog_inputs: int = 0
    features: CapabilitiesFeatures | None = None

    @property
    def has_watchdog(self) -> bool:
        return bool(self.features and self.features.watchdog)

    @property
    def has_rle(self) -> bool:
        return bool(self.features and self.features.rle)

    @property
    def debug_frames(self) -> bool:
        return bool(self.features and self.features.debug_frames)

    @property
    def debug_io(self) -> bool:
        return bool(self.features and self.features.debug_io)

    @property
    def has_eeprom(self) -> bool:
        return bool(self.features and self.features.eeprom)

    @property
    def has_dac(self) -> bool:
        return bool(self.features and self.features.dac)

    @property
    def has_hw_serial1(self) -> bool:
        return bool(self.features and self.features.hw_serial1)

    @property
    def has_fpu(self) -> bool:
        return bool(self.features and self.features.fpu)

    @property
    def is_3v3_logic(self) -> bool:
        return bool(self.features and self.features.logic_3v3)

    @property
    def has_large_buffer(self) -> bool:
        return bool(self.features and self.features.large_buffer)

    @property
    def has_i2c(self) -> bool:
        return bool(self.features and self.features.i2c)

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary including expanded boolean flags."""
        res = msgspec.structs.asdict(self)
        res.update({
            "has_watchdog": self.has_watchdog,
            "has_rle": self.has_rle,
            "has_eeprom": self.has_eeprom,
            "has_dac": self.has_dac,
            "has_hw_serial1": self.has_hw_serial1,
            "has_fpu": self.has_fpu,
            "is_3v3_logic": self.is_3v3_logic,
            "has_large_buffer": self.has_large_buffer,
            "has_i2c": self.has_i2c,
        })
        return res


class SerialThroughputStats(msgspec.Struct):
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

    def as_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


# [EXTENDED METRICS] Latency histogram bucket boundaries in milliseconds
LATENCY_BUCKETS_MS: tuple[float, ...] = (5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)


def _latency_bucket_counts_factory() -> list[int]:
    return [0] * len(LATENCY_BUCKETS_MS)


class SerialLatencyStats(msgspec.Struct):
    """RPC command latency histogram."""

    bucket_counts: list[int] = msgspec.field(default_factory=_latency_bucket_counts_factory)
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


class SerialFlowStats(msgspec.Struct):
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

    def as_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


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
