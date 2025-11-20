"""Utility helpers shared across Yun Bridge packages."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from struct import pack as struct_pack, unpack as struct_unpack
from typing import Final, Protocol, Tuple, TypeVar, cast

from cobs import cobs as _cobs  # type: ignore[import]

from typing import Optional

from yunbridge.rpc.protocol import MAX_PAYLOAD_SIZE

from .const import ALLOWED_COMMAND_WILDCARD


class _CobsCodec(Protocol):
    def encode(self, data: bytes) -> bytes:
        ...

    def decode(self, data: bytes) -> bytes:
        ...


_COBC_MODULE: _CobsCodec = cast(_CobsCodec, _cobs)

DecodeError = getattr(_COBC_MODULE, "DecodeError", ValueError)

T = TypeVar("T")


def cobs_encode(data: bytes) -> bytes:
    """COBS-encode *data* using the upstream library."""

    return _COBC_MODULE.encode(data)


def cobs_decode(data: bytes) -> bytes:
    """COBS-decode *data* using the upstream library."""

    return _COBC_MODULE.decode(data)


def pack_u16(value: int) -> bytes:
    """Pack ``value`` as big-endian unsigned 16-bit."""

    return struct_pack(">H", value & 0xFFFF)


def unpack_u16(data: bytes) -> int:
    """Decode the first two bytes of ``data`` as big-endian unsigned 16-bit."""

    if len(data) < 2:
        raise ValueError("payload shorter than 2 bytes for u16 unpack")
    return struct_unpack(">H", data[:2])[0]


def clamp(value: int, minimum: int, maximum: int) -> int:
    """Return *value* constrained to the ``[minimum, maximum]`` range."""

    return max(minimum, min(maximum, value))


def chunk_payload(data: bytes, max_size: int) -> tuple[bytes, ...]:
    """Split *data* in chunks of at most ``max_size`` bytes."""

    if max_size <= 0:
        raise ValueError("max_size must be positive")
    if not data:
        return tuple()
    return tuple(
        data[index:index + max_size]
        for index in range(0, len(data), max_size)
    )


def normalise_allowed_commands(commands: Iterable[str]) -> Tuple[str, ...]:
    """Return a deduplicated, lower-cased allow-list preserving wildcards."""

    seen: set[str] = set()
    normalised: list[str] = []
    for item in commands:
        candidate = item.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered == ALLOWED_COMMAND_WILDCARD:
            return (ALLOWED_COMMAND_WILDCARD,)
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(lowered)
    return tuple(normalised)


def deduplicate(sequence: Sequence[T]) -> tuple[T, ...]:
    """Return ``sequence`` without duplicates, preserving order."""

    return tuple(dict.fromkeys(sequence))


def encode_status_reason(reason: Optional[str]) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits."""

    if not reason:
        return b""
    payload = reason.encode("utf-8", errors="ignore")
    return payload[:MAX_PAYLOAD_SIZE]


__all__: Final[tuple[str, ...]] = (
    "DecodeError",
    "cobs_encode",
    "cobs_decode",
    "normalise_allowed_commands",
    "pack_u16",
    "unpack_u16",
    "clamp",
    "chunk_payload",
    "deduplicate",
    "encode_status_reason",
)
