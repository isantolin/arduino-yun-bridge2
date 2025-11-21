from __future__ import annotations

from enum import IntEnum


class PacketTypes(IntEnum):
    CONNECT = 1
    PUBLISH = 3


__all__ = ["PacketTypes"]
