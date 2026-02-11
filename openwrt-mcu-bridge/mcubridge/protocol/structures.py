"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Improved robustness for binary parsing (SIL-2).
"""

from __future__ import annotations
import struct
from typing import TypeVar, Type, Any
import msgspec

T = TypeVar("T", bound="BaseStruct")

class BaseStruct(msgspec.Struct, frozen=True):
    @classmethod
    def parse(cls: Type[T], data: bytes) -> T:
        if not data:
            raise ValueError("Empty payload")
        try:
            return cls._decode(data)
        except (IndexError, struct.error, ValueError) as e:
            raise ValueError(f"Malformed payload: {e}") from e

    @classmethod
    def _decode(cls: Type[T], data: bytes) -> T:
        raise NotImplementedError

# --- Binary Protocol Packets ---

class FileWritePacket(BaseStruct, frozen=True):
    path: str
    data: bytes
    @classmethod
    def _decode(cls, data: bytes) -> FileWritePacket:
        path_len = data[0]
        path = data[1:1+path_len].decode("utf-8")
        data_len = struct.unpack(">H", data[1+path_len:3+path_len])[0]
        return FileWritePacket(path=path, data=data[3+path_len:3+path_len+data_len])

class FileReadPacket(BaseStruct, frozen=True):
    path: str
    @classmethod
    def _decode(cls, data: bytes) -> FileReadPacket:
        return FileReadPacket(path=data[1:1+data[0]].decode("utf-8"))

class FileRemovePacket(BaseStruct, frozen=True):
    path: str
    @classmethod
    def _decode(cls, data: bytes) -> FileRemovePacket:
        return FileRemovePacket(path=data[1:1+data[0]].decode("utf-8"))

class VersionResponsePacket(BaseStruct, frozen=True):
    major: int
    minor: int
    @classmethod
    def _decode(cls, data: bytes) -> VersionResponsePacket:
        return VersionResponsePacket(major=data[0], minor=data[1])

class FreeMemoryResponsePacket(BaseStruct, frozen=True):
    value: int
    @classmethod
    def _decode(cls, data: bytes) -> FreeMemoryResponsePacket:
        return FreeMemoryResponsePacket(value=struct.unpack(">H", data[:2])[0])

class DigitalReadResponsePacket(BaseStruct, frozen=True):
    value: int
    @classmethod
    def _decode(cls, data: bytes) -> DigitalReadResponsePacket:
        return DigitalReadResponsePacket(value=data[0])

class AnalogReadResponsePacket(BaseStruct, frozen=True):
    value: int
    @classmethod
    def _decode(cls, data: bytes) -> AnalogReadResponsePacket:
        return AnalogReadResponsePacket(value=struct.unpack(">H", data[:2])[0])

class DatastoreGetPacket(BaseStruct, frozen=True):
    key: str
    @classmethod
    def _decode(cls, data: bytes) -> DatastoreGetPacket:
        return DatastoreGetPacket(key=data[1:1+data[0]].decode("utf-8"))

class DatastorePutPacket(BaseStruct, frozen=True):
    key: str
    value: bytes
    @classmethod
    def _decode(cls, data: bytes) -> DatastorePutPacket:
        key_len = data[0]
        key = data[1:1+key_len].decode("utf-8")
        val_len = data[1+key_len]
        return DatastorePutPacket(key=key, value=data[2+key_len:2+key_len+val_len])

class MailboxPushPacket(BaseStruct, frozen=True):
    data: bytes
    @classmethod
    def _decode(cls, data: bytes) -> MailboxPushPacket:
        msg_len = struct.unpack(">H", data[:2])[0]
        return MailboxPushPacket(data=data[2:2+msg_len])

# --- High-Level Structure ---

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
