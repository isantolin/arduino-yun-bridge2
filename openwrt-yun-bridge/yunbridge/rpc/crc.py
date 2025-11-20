"""CRC-16 helpers backed by :mod:`binascii`."""

from __future__ import annotations

from binascii import crc_hqx


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    """Return the CRC-16-CCITT of *data* using ``binascii.crc_hqx``."""

    return crc_hqx(data, initial)
