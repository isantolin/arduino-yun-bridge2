"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Improved robustness for binary parsing (SIL-2) using Construct + Msgspec.
"""

from __future__ import annotations
from typing import TypeVar, Type, Any, ClassVar
import msgspec
from construct import (  # type: ignore
    Struct,
    Int8ub,
    Int16ub,
    PascalString,
    PrefixedBytes,
    Construct,
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
        container = cls._SCHEMA.parse(data)
        
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

    _SCHEMA = Struct(
        "path" / PascalString(Int8ub, "utf-8"),
        "data" / PrefixedBytes(Int16ub)
    )


class FileReadPacket(BaseStruct, frozen=True):
    path: str

    _SCHEMA = Struct(
        "path" / PascalString(Int8ub, "utf-8")
    )


class FileRemovePacket(BaseStruct, frozen=True):
    path: str

    _SCHEMA = Struct(
        "path" / PascalString(Int8ub, "utf-8")
    )


class VersionResponsePacket(BaseStruct, frozen=True):
    major: int
    minor: int

    _SCHEMA = Struct(
        "major" / Int8ub,
        "minor" / Int8ub
    )


class FreeMemoryResponsePacket(BaseStruct, frozen=True):
    value: int

    _SCHEMA = Struct(
        "value" / Int16ub
    )


class DigitalReadResponsePacket(BaseStruct, frozen=True):
    value: int

    _SCHEMA = Struct(
        "value" / Int8ub
    )


class AnalogReadResponsePacket(BaseStruct, frozen=True):
    value: int

    _SCHEMA = Struct(
        "value" / Int16ub
    )


class DatastoreGetPacket(BaseStruct, frozen=True):
    key: str

    _SCHEMA = Struct(
        "key" / PascalString(Int8ub, "utf-8")
    )


class DatastorePutPacket(BaseStruct, frozen=True):
    key: str
    value: bytes

    _SCHEMA = Struct(
        "key" / PascalString(Int8ub, "utf-8"),
        "value" / PrefixedBytes(Int8ub)
    )


class MailboxPushPacket(BaseStruct, frozen=True):
    data: bytes

    _SCHEMA = Struct(
        "data" / PrefixedBytes(Int16ub)
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