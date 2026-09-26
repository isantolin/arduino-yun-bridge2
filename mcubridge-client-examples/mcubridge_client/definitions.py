from __future__ import annotations

import os
from enum import IntEnum

DEFAULT_GATEWAY_HOST: str = "127.0.0.1"
DEFAULT_GATEWAY_PORT: int = 8443
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
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> dict[str, str | int]:
    """Build Bridge constructor keyword arguments from CLI/env parameters targeting Gateway."""
    args: dict[str, str | int] = {}
    effective_host = (
        host
        or os.environ.get("MCUBRIDGE_GATEWAY_HOST")
        or os.environ.get("MCUBRIDGE_CLOUD_HOST")
        or DEFAULT_GATEWAY_HOST
    )
    raw_port = (
        port
        or os.environ.get("MCUBRIDGE_GATEWAY_PORT")
        or os.environ.get("MCUBRIDGE_CLOUD_PORT")
        or DEFAULT_GATEWAY_PORT
    )
    effective_port = int(raw_port)
    effective_device = device_id or os.environ.get("MCUBRIDGE_DEVICE_ID")
    if not effective_device:
        raise ValueError(
            "Explicit target device_id is required (pass device_id or set MCUBRIDGE_DEVICE_ID). "
            "Implicit fallback is prohibited."
        )

    args["host"] = effective_host
    args["port"] = effective_port
    args["device_id"] = effective_device
    if topic_prefix:
        args["topic_prefix"] = topic_prefix
    return args
