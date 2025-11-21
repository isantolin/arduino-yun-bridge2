from __future__ import annotations

from enum import IntEnum


class ProtocolVersion(IntEnum):
    V31 = 0x03
    V311 = 0x04
    V5 = 0x05


__all__ = ["ProtocolVersion"]
