"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Improved robustness for binary parsing (SIL-2).
"""

from __future__ import annotations
from typing import TypeVar, Type, Any
import msgspec

T = TypeVar("T", bound="BaseStruct")


class BaseStruct(msgspec.Struct, frozen=True):
    @classmethod
    def parse(cls: Type[T], data: bytes | bytearray | memoryview) -> T:
        if not data:
            raise ValueError("Empty payload")
        # Ensure we work with a memoryview to avoid slicing copies during parsing
        mv = memoryview(data)
        try:
            return cls._decode(mv)
        except (IndexError, ValueError) as e:
            raise ValueError(f"Malformed payload: {e}") from e

    @classmethod
    def _decode(cls: Type[T], data: memoryview) -> T:
        raise NotImplementedError


# --- Binary Protocol Packets ---


class FileWritePacket(BaseStruct, frozen=True):
    path: str
    data: bytes

    @classmethod
    def _decode(cls, data: memoryview) -> FileWritePacket:
        if len(data) < 3:
            raise ValueError("Too short")
        path_len = data[0]
        if len(data) < 3 + path_len:
            raise ValueError("Truncated path/header")
        path = bytes(data[1 : 1 + path_len]).decode("utf-8")
        # data_len was >H (2 bytes big endian)
        data_len = int.from_bytes(data[1 + path_len : 3 + path_len], "big")
        file_data = data[3 + path_len : 3 + path_len + data_len]
        if len(file_data) < data_len:
            raise ValueError("Truncated data")
        return FileWritePacket(path=path, data=file_data.tobytes())


class FileReadPacket(BaseStruct, frozen=True):
    path: str

    @classmethod
    def _decode(cls, data: memoryview) -> FileReadPacket:
        if len(data) < 1:
            raise ValueError("Too short")
        path_len = data[0]
        if len(data) < 1 + path_len:
            raise ValueError("Truncated path")
        return FileReadPacket(path=bytes(data[1 : 1 + path_len]).decode("utf-8"))


class FileRemovePacket(BaseStruct, frozen=True):
    path: str

    @classmethod
    def _decode(cls, data: memoryview) -> FileRemovePacket:
        if len(data) < 1:
            raise ValueError("Too short")
        path_len = data[0]
        if len(data) < 1 + path_len:
            raise ValueError("Truncated path")
        return FileRemovePacket(path=bytes(data[1 : 1 + path_len]).decode("utf-8"))


class VersionResponsePacket(BaseStruct, frozen=True):
    major: int
    minor: int

    @classmethod
    def _decode(cls, data: memoryview) -> VersionResponsePacket:
        if len(data) < 2:
            raise ValueError("Too short")
        return VersionResponsePacket(major=data[0], minor=data[1])


class FreeMemoryResponsePacket(BaseStruct, frozen=True):
    value: int

    @classmethod
    def _decode(cls, data: memoryview) -> FreeMemoryResponsePacket:
        if len(data) < 2:
            raise ValueError("Too short")
        return FreeMemoryResponsePacket(value=int.from_bytes(data[:2], "big"))


class DigitalReadResponsePacket(BaseStruct, frozen=True):
    value: int

    @classmethod
    def _decode(cls, data: memoryview) -> DigitalReadResponsePacket:
        if len(data) < 1:
            raise ValueError("Too short")
        return DigitalReadResponsePacket(value=data[0])


class AnalogReadResponsePacket(BaseStruct, frozen=True):
    value: int

    @classmethod
    def _decode(cls, data: memoryview) -> AnalogReadResponsePacket:
        if len(data) < 2:
            raise ValueError("Too short")
        return AnalogReadResponsePacket(value=int.from_bytes(data[:2], "big"))


class DatastoreGetPacket(BaseStruct, frozen=True):
    key: str

    @classmethod
    def _decode(cls, data: memoryview) -> DatastoreGetPacket:
        if len(data) < 1:
            raise ValueError("Too short")
        key_len = data[0]
        if len(data) < 1 + key_len:
            raise ValueError("Truncated key")
        return DatastoreGetPacket(key=bytes(data[1 : 1 + key_len]).decode("utf-8"))


class DatastorePutPacket(BaseStruct, frozen=True):
    key: str
    value: bytes

    @classmethod
    def _decode(cls, data: memoryview) -> DatastorePutPacket:
        if len(data) < 1:
            raise ValueError("Too short")
        key_len = data[0]
        if len(data) < 1 + key_len + 1:
            raise ValueError("Truncated key/value header")
        key = bytes(data[1 : 1 + key_len]).decode("utf-8")
        val_len = data[1 + key_len]
        if len(data) < 1 + key_len + 1 + val_len:
            raise ValueError("Truncated value")
        val_bytes = data[2 + key_len : 2 + key_len + val_len].tobytes()
        return DatastorePutPacket(key=key, value=val_bytes)


class MailboxPushPacket(BaseStruct, frozen=True):
    data: bytes

    @classmethod
    def _decode(cls, data: memoryview) -> MailboxPushPacket:
        if len(data) < 2:
            raise ValueError("Too short")
        msg_len = int.from_bytes(data[:2], "big")
        if len(data) < 2 + msg_len:
            raise ValueError("Truncated message")
        return MailboxPushPacket(data=data[2 : 2 + msg_len].tobytes())


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
