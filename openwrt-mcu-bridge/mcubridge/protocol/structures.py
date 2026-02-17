"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Improved robustness for binary parsing (SIL-2) using Construct + Msgspec.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
from enum import IntEnum
from typing import Any, ClassVar, Self, Type, TypedDict, TypeVar, cast

import msgspec
from construct import (  # type: ignore
    Bytes,
    Construct,
    GreedyBytes,
    Int8ub,
    Int16ub,
    PascalString,
    Prefixed,  # type: ignore
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

# [SIL-2] Construct Schema for Full Frame
# Reuses the header definition from protocol.py to ensure consistency.
FRAME_STRUCT = BinStruct(
    "header" / protocol.CRC_COVERED_HEADER_STRUCT,
    "payload" / Bytes(this.header.payload_len),
    "crc" / protocol.CRC_STRUCT,
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


class HandshakeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    synchronised: bool = False
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    last_error: str | None = None
    last_unix: float = 0.0


class SerialLinkSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    connected: bool = False
    synchronised: bool = False


class BridgeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    serial_link: SerialLinkSnapshot
    handshake: HandshakeSnapshot
    mcu_version: dict[str, int] | None = None
    capabilities: dict[str, Any] | None = None


# --- MQTT Spool Structures ---


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


class SpoolRecord(TypedDict, total=False):
    topic_name: str
    payload: str
    qos: int
    retain: bool
    content_type: str | None
    payload_format_indicator: int | None
    message_expiry_interval: int | None
    response_topic: str | None
    correlation_data: str | None
    user_properties: list[Any]


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
        return {
            "topic_name": self.topic_name,
            "payload": base64.b64encode(self.payload).decode("ascii"),
            "qos": int(self.qos),
            "retain": self.retain,
            "content_type": self.content_type,
            "payload_format_indicator": self.payload_format_indicator,
            "message_expiry_interval": self.message_expiry_interval,
            "response_topic": self.response_topic,
            "correlation_data": (
                base64.b64encode(self.correlation_data).decode("ascii") if self.correlation_data is not None else None
            ),
            "user_properties": list(self.user_properties),
        }

    @classmethod
    def from_record(cls, record: SpoolRecord) -> Self:
        payload_b64 = str(record.get("payload", ""))
        payload = base64.b64decode(payload_b64.encode("ascii"))
        correlation_raw = record.get("correlation_data")
        correlation_data: bytes | None = None
        if correlation_raw is not None:
            encoded = str(correlation_raw).encode("ascii")
            correlation_data = base64.b64decode(encoded)
        raw_properties = record.get("user_properties")
        user_properties: tuple[UserProperty, ...] = _normalize_user_properties(raw_properties)
        return cls(
            topic_name=str(record.get("topic_name", "")),
            payload=payload,
            qos=int(record.get("qos", 0)),
            retain=bool(record.get("retain", False)),
            content_type=record.get("content_type"),
            payload_format_indicator=record.get("payload_format_indicator"),
            message_expiry_interval=record.get("message_expiry_interval"),
            response_topic=record.get("response_topic"),
            correlation_data=correlation_data,
            user_properties=user_properties,
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
