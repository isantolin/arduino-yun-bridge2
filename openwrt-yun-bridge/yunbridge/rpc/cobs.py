"""Thin wrappers around the upstream ``python-cobs`` helpers."""

from __future__ import annotations

from typing import Any, Iterable, Iterator, cast

from cobs import cobs as _cobs  # type: ignore[import]

COBS_MODULE = cast(Any, _cobs)


def encode(data: bytes) -> bytes:
    """COBS-encode *data* using the upstream module."""

    return bytes(COBS_MODULE.encode(data))


def decode(encoded: bytes) -> bytes:
    """Decode a COBS frame using the upstream module."""

    return bytes(COBS_MODULE.decode(encoded))


def iter_decode(stream: Iterable[int]) -> Iterator[bytes]:
    """Yield decoded packets from an iterable of bytes with zero delimiters."""

    packet = bytearray()
    for byte in stream:
        if byte == 0:
            if packet:
                yield decode(bytes(packet))
                packet.clear()
        else:
            packet.append(byte)

    if packet:
        yield decode(bytes(packet))
