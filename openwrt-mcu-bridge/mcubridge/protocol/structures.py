"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Improved robustness for binary parsing (SIL-2) using Construct + Msgspec.
"""

from __future__ import annotations

import asyncio
import time
from binascii import crc32
from enum import IntEnum
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

import construct as construct_raw
import msgspec
from construct import ConstructError

from . import protocol

if TYPE_CHECKING:
    import collections
    construct: Any = construct_raw
    Construct = construct_raw.Construct[Any]
else:
    construct = construct_raw
    Construct = construct_raw.Construct

BinStruct: Final = construct.Struct

__all__ = [
    "collect_system_metrics",
    "BridgeStatus",
    "BridgeSnapshot",
    "HandshakeSnapshot",
    "SerialLinkSnapshot",
    "SerialPipelineSnapshot",
    "McuVersion",
    "McuCapabilities",
    "SerialFlowStats",
    "SerialThroughputStats",
    "SerialLatencyStats",
    "SupervisorStats",
    "SupervisorSnapshot",
    "QueuedPublish",
    "SpoolRecord",
    "ProcessOutputBatch",
    "PendingCommand",
    "BaseStruct",
    "FileWritePacket",
    "FileReadPacket",
    "FileReadResponsePacket",
    "FileRemovePacket",
    "VersionResponsePacket",
    "FreeMemoryResponsePacket",
    "DigitalReadResponsePacket",
    "AnalogReadResponsePacket",
    "DatastoreGetPacket",
    "DatastoreGetResponsePacket",
    "DatastorePutPacket",
    "MailboxPushPacket",
    "MailboxProcessedPacket",
    "MailboxAvailableResponsePacket",
    "MailboxReadResponsePacket",
    "PinModePacket",
    "DigitalWritePacket",
    "AnalogWritePacket",
    "PinReadPacket",
    "AckPacket",
    "ConsoleWritePacket",
    "ProcessRunPacket",
    "ProcessRunAsyncPacket",
    "ProcessKillPacket",
    "ProcessPollPacket",
    "ProcessRunResponsePacket",
    "ProcessRunAsyncResponsePacket",
    "ProcessPollResponsePacket",
    "HandshakeConfigPacket",
    "CapabilitiesPacket",
    "SetBaudratePacket",
    "Frame",
    "SlidingWindowStats",
    "SpoolSnapshot",
    "UINT8_STRUCT",
    "UINT16_STRUCT",
    "UINT32_STRUCT",
    "NONCE_COUNTER_STRUCT",
    "CRC_STRUCT",
    "FRAME_STRUCT",
]

# --- Basic Binary Types ---
UINT8_STRUCT: Final = construct.Int8ub
UINT16_STRUCT: Final = construct.Int16ub
UINT32_STRUCT: Final = construct.Int32ub
NONCE_COUNTER_STRUCT: Final = construct.Int64ub
CRC_STRUCT: Final = construct.Int32ub

# [SIL-2] Explicit Command ID Structure
_RawCommandIdStruct: Final = construct.BitStruct(
    "compressed" / construct.Flag,
    "id"
    / construct.Enum(
        construct.BitsInteger(15),
        protocol.Command,
        protocol.Status,
        _default=construct.Pass,
    ),
)

class CommandIdAdapter(construct.Adapter):
    def _decode(self, obj: Any, context: Any, path: Any) -> int:
        cmd_val = int(obj.id)
        if obj.compressed:
            cmd_val |= protocol.CMD_FLAG_COMPRESSED
        return cmd_val

    def _encode(self, obj: int, context: Any, path: Any) -> dict[str, Any]:
        return {
            "compressed": bool(obj & protocol.CMD_FLAG_COMPRESSED),
            "id": obj & ~protocol.CMD_FLAG_COMPRESSED,
        }

CommandIdStruct: Final = CommandIdAdapter(_RawCommandIdStruct)

CRC_COVERED_HEADER_STRUCT: Final = BinStruct(
    "version" / construct.Int8ub,
    "payload_len" / construct.Int16ub,
    "command_id" / construct.Int16ub,
)

T = TypeVar("T", bound="BaseStruct")

class BaseStruct(msgspec.Struct, frozen=True):
    _SCHEMA: ClassVar[Construct]

    @classmethod
    def decode(cls: Type[T], data: bytes | bytearray | memoryview) -> T:
        if not data:
            raise ValueError("Empty payload")
        try:
            b_data = bytes(data)
            container: Any = cls._SCHEMA.parse(b_data)
            if cls._SCHEMA.build(container) != b_data:
                raise ValueError("Payload length mismatch")
        except Exception as e:
            raise ConstructError(str(e)) from e
        return msgspec.convert(container, cls)

    def encode(self) -> bytes:
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

def _validate_ack_timeout(ctx: Any) -> bool:
    return (
        protocol.HANDSHAKE_ACK_TIMEOUT_MIN_MS
        <= ctx.ack_timeout_ms
        <= protocol.HANDSHAKE_ACK_TIMEOUT_MAX_MS
    )

def _validate_ack_retry_limit(ctx: Any) -> bool:
    return (
        protocol.HANDSHAKE_RETRY_LIMIT_MIN
        <= ctx.ack_retry_limit
        <= protocol.HANDSHAKE_RETRY_LIMIT_MAX
    )

def _validate_response_timeout(ctx: Any) -> bool:
    return (
        protocol.HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS
        <= ctx.response_timeout_ms
        <= protocol.HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS
    )

class HandshakeConfigPacket(BaseStruct, frozen=True):
    ack_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]
    ack_retry_limit: Annotated[int, msgspec.Meta(ge=0)]
    response_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]

    _SCHEMA = BinStruct(
        "ack_timeout_ms" / construct.Int16ub,
        "ack_retry_limit" / construct.Int8ub,
        "response_timeout_ms" / construct.Int32ub,
        # [SIL-2] Declarative Protocol Validation
        construct.Check(_validate_ack_timeout),
        construct.Check(_validate_ack_retry_limit),
        construct.Check(_validate_response_timeout),
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
    big_buffer: bool
    i2c: bool
    spi: bool

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
        "feat"
        / construct.BitStruct(
            construct.Padding(32 - 12),
            "spi" / construct.Flag,
            "i2c" / construct.Flag,
            "big_buffer" / construct.Flag,
            "logic_3v3" / construct.Flag,
            "fpu" / construct.Flag,
            "hw_serial1" / construct.Flag,
            "dac" / construct.Flag,
            "eeprom" / construct.Flag,
            "debug_io" / construct.Flag,
            "debug_frames" / construct.Flag,
            "rle" / construct.Flag,
            "watchdog" / construct.Flag,
        ),
    )

class SetBaudratePacket(BaseStruct, frozen=True):
    baudrate: Annotated[int, msgspec.Meta(ge=0)]
    _SCHEMA = BinStruct("baudrate" / construct.Int32ub)

def _compute_crc32(data: Any) -> int:
    return crc32(cast(bytes, data)) & 0xFFFFFFFF

FRAME_STRUCT = BinStruct(
    "content" / construct.RawCopy(
        BinStruct(
            "header" / CRC_COVERED_HEADER_STRUCT,
            "payload" / construct.Bytes(construct.this.header.payload_len),
        )
    ),
    "crc" / construct.Checksum(CRC_STRUCT, _compute_crc32, construct.this.content.data),
)

class MqttPayload(msgspec.Struct, frozen=True):
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False
    properties: dict[str, Any] = msgspec.field(default_factory=lambda: cast(dict[str, Any], {}))

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

class QOSLevel(IntEnum):
    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2

UserProperty = tuple[str, str]

class SpoolRecord(msgspec.Struct, omit_defaults=True):
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
    user_properties: list[UserProperty] = msgspec.field(default_factory=lambda: cast(list[UserProperty], []))

class QueuedPublish(msgspec.Struct):
    topic_name: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: list[UserProperty] = msgspec.field(default_factory=lambda: cast(list[UserProperty], []))

    def to_record(self) -> SpoolRecord:
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
        data: dict[str, Any] = record if isinstance(record, dict) else msgspec.structs.asdict(record)
        return cls(
            topic_name=str(data.get("topic_name", "")),
            payload=data.get("payload", b""),
            qos=int(data.get("qos", 0)),
            retain=bool(data.get("retain", False)),
            content_type=data.get("content_type"),
            user_properties=data.get("user_properties", []),
        )

class ProcessOutputBatch(msgspec.Struct):
    status_byte: int
    exit_code: int
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool

class QueueEvent(msgspec.Struct):
    truncated_bytes: int = 0
    dropped_chunks: int = 0
    dropped_bytes: int = 0
    accepted: bool = False

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

    def mark_success(self) -> None:
        self.success = True
        self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        self.completion.set()

class BaseStats(msgspec.Struct):
    def as_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)

class SupervisorSnapshot(msgspec.Struct):
    restarts: Annotated[int, msgspec.Meta(ge=0)]
    last_failure_unix: float
    last_exception: str | None
    backoff_seconds: Annotated[float, msgspec.Meta(ge=0.0)]
    fatal: bool

class SupervisorStats(BaseStats):
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
    protocol_version: int = 0
    board_arch: int = 0
    num_digital_pins: int = 0
    num_analog_inputs: int = 0
    features: CapabilitiesFeatures | None = None

    def as_dict(self) -> dict[str, Any]:
        res = msgspec.structs.asdict(self)
        for name in dir(self.__class__):
            if isinstance(getattr(self.__class__, name), property):
                res[name] = getattr(self, name)
        return res

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

LATENCY_BUCKETS_MS: tuple[float, ...] = (
    5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0
)

class SerialLatencyStats(msgspec.Struct):
    bucket_counts: list[int] = msgspec.field(default_factory=lambda: [0] * len(LATENCY_BUCKETS_MS))
    overflow_count: int = 0
    total_observations: int = 0
    total_latency_ms: float = 0.0
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0

    def initialize_prometheus(self, registry: Any | None = None) -> None:
        """Initialize native prometheus Summary/Histogram for RPC latency."""
        from prometheus_client import Summary
        Summary(
            "mcubridge_rpc_latency_seconds",
            "RPC command round-trip latency",
            registry=registry,
        )

    def record(self, latency_ms: float) -> None:
        self.total_observations += 1
        self.total_latency_ms += latency_ms
        self.min_latency_ms = min(self.min_latency_ms, latency_ms)
        self.max_latency_ms = max(self.max_latency_ms, latency_ms)
        for i, bucket in enumerate(LATENCY_BUCKETS_MS):
            if latency_ms <= bucket:
                self.bucket_counts[i] += 1
        if latency_ms > LATENCY_BUCKETS_MS[-1]:
            self.overflow_count += 1

    def as_dict(self) -> dict[str, Any]:
        avg = self.total_latency_ms / self.total_observations if self.total_observations > 0 else 0.0
        return {
            "avg_ms": avg,
            "count": self.total_observations,
            "min_ms": self.min_latency_ms if self.total_observations > 0 else 0.0,
            "max_ms": self.max_latency_ms,
        }

class McuVersion(msgspec.Struct):
    major: int
    minor: int

class SerialPipelineSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    inflight: dict[str, Any] | None = None
    last_completion: dict[str, Any] | None = None

class SerialLinkSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    connected: bool = False
    synchronised: bool = False

class HandshakeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    synchronised: bool = False
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    last_error: str | None = None
    last_duration: float = 0.0

class BridgeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    serial_link: SerialLinkSnapshot
    handshake: HandshakeSnapshot
    serial_pipeline: SerialPipelineSnapshot
    serial_flow: dict[str, Any]
    mcu_version: McuVersion | None = None
    capabilities: dict[str, Any] | None = None

class SerialFlowSnapshot(msgspec.Struct):
    commands_sent: int
    commands_acked: int
    retries: int
    failures: int
    last_event_unix: float

class SerialFlowStats(BaseStats):
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

class Frame(BaseStruct, frozen=True):
    command_id: int
    payload: bytes
    crc: int
    _SCHEMA = BinStruct(
        "command_id" / construct.Int16ub,
        "payload" / construct.GreedyBytes,
        "crc" / construct.Int32ub,
    )

class SlidingWindowStats:
    def __init__(self, window_size: int = 100) -> None:
        import collections
        self.window: collections.deque[float] = collections.deque(maxlen=window_size)

    def record(self, value: float) -> None:
        self.window.append(float(value))

    @property
    def avg(self) -> float:
        return sum(self.window) / len(self.window) if self.window else 0.0

    def as_snapshot(self) -> dict[str, Any]:
        return {"avg": self.avg, "count": len(self.window)}

    def initialize_prometheus(self, registry: Any) -> None:
        pass

class SpoolSnapshot(msgspec.Struct, frozen=True):
    enabled: bool
    size: int = 0
    max_size: int = 0
    errors: int = 0
    recoveries: int = 0

def collect_system_metrics() -> dict[str, Any]:
    """Collect system-level metrics using psutil."""
    import psutil
    result: dict[str, Any] = {}
    try:
        proc = psutil.Process()
        with proc.oneshot():
            result["cpu_percent"] = psutil.cpu_percent(interval=None)
            result["cpu_count"] = psutil.cpu_count() or 1
            mem = psutil.virtual_memory()
            result["memory_total_bytes"] = mem.total
            result["memory_available_bytes"] = mem.available
            result["memory_percent"] = mem.percent
            load = psutil.getloadavg()
            result["load_avg_1m"] = load[0]
            result["load_avg_5m"] = load[1]
            result["load_avg_15m"] = load[2]
            temps = psutil.sensors_temperatures()
            names = ("cpu_thermal", "coretemp", "soc_thermal")
            cpu_temp = next((temps[n][0].current for n in names if n in temps and temps[n]), None)
            result["temperature_celsius"] = cpu_temp
    except Exception:
        pass
    return result

class BridgeStatus(msgspec.Struct):
    serial_connected: bool
    serial_flow: SerialFlowSnapshot
    link_synchronised: bool
    handshake_attempts: int
    handshake_successes: int
    handshake_failures: int
    handshake_last_error: str | None
    handshake_last_unix: float
    mqtt_queue_size: int
    mqtt_queue_limit: int
    mqtt_messages_dropped: int
    mqtt_drop_counts: dict[str, int]
    mqtt_spooled_messages: int
    mqtt_spooled_replayed: int
    mqtt_spool_errors: int
    mqtt_spool_degraded: bool
    mqtt_spool_failure_reason: str | None
    mqtt_spool_retry_attempts: int
    mqtt_spool_backoff_until: float
    mqtt_spool_last_error: str | None
    mqtt_spool_recoveries: int
    mqtt_spool_pending: int
    file_storage_root: str
    file_storage_bytes_used: int
    file_storage_quota_bytes: int
    file_write_max_bytes: int
    file_write_limit_rejections: int
    file_storage_limit_rejections: int
    datastore_keys: list[str]
    mailbox_size: int
    mailbox_bytes: int
    mailbox_dropped_messages: int
    mailbox_dropped_bytes: int
    mailbox_truncated_messages: int
    mailbox_truncated_bytes: int
    mailbox_incoming_dropped_messages: int
    mailbox_incoming_dropped_bytes: int
    mailbox_incoming_truncated_messages: int
    mailbox_incoming_truncated_bytes: int
    console_queue_size: int
    console_queue_bytes: int
    console_dropped_chunks: int
    console_dropped_bytes: int
    console_truncated_chunks: int
    console_truncated_bytes: int
    mcu_paused: bool
    mcu_version: McuVersion | None
    watchdog_enabled: bool
    watchdog_interval: float
    watchdog_beats: int
    watchdog_last_beat: float
    running_processes: list[str]
    allowed_commands: list[str]
    config_source: str
    bridge: BridgeSnapshot
    supervisors: dict[str, SupervisorSnapshot]
    heartbeat_unix: float
