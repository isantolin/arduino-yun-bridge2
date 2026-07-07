from __future__ import annotations

from enum import IntEnum

from mcubridge.protocol.structures import UserProperty
from mcubridge.protocol.mcubridge_pb2 import CloudQueuedPublish
from .protocol import MAX_PAYLOAD_SIZE

DEFAULT_SOCKET_PATH: str = "/var/run/mcubridge.sock"
DEFAULT_TOPIC_PREFIX: str = "br"


class SpiBitOrder(IntEnum):
    """SPI Bit transmission order."""

    LSBFIRST = 0
    MSBFIRST = 1


class SpiMode(IntEnum):
    """SPI Data modes (CPOL/CPHA combinations)."""

    MODE0 = 0
    MODE1 = 1
    MODE2 = 2
    MODE3 = 3


def build_bridge_args(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> dict[str, object]:
    """Build Bridge constructor keyword arguments from CLI/env parameters."""
    args: dict[str, object] = {}
    if socket_path:
        args["socket_path"] = socket_path
    if topic_prefix:
        args["topic_prefix"] = topic_prefix
    return args


__all__ = [
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_TOPIC_PREFIX",
    "MAX_PAYLOAD_SIZE",
    "CloudQueuedPublish",
    "SpiBitOrder",
    "SpiMode",
    "UserProperty",
    "build_bridge_args",
]
