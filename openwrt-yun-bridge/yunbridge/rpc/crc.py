"""CRC-32 helpers backed by :mod:`binascii`."""

from __future__ import annotations

from binascii import crc32


def crc32_ieee(data: bytes, initial: int = 0x0) -> int:
    """Return the IEEE CRC-32 of *data* using ``binascii.crc32``."""

    return crc32(data, initial) & 0xFFFFFFFF
