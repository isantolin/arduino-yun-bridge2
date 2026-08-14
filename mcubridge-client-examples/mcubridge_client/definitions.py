from __future__ import annotations

from enum import IntEnum


import os

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
    effective_socket = socket_path or os.environ.get("MCUBRIDGE_SOCKET_PATH") or DEFAULT_SOCKET_PATH
    if effective_socket:
        args["socket_path"] = effective_socket
    if topic_prefix:
        args["topic_prefix"] = topic_prefix
    return args
