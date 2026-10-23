from __future__ import annotations

import os

from .protocol import CLOUD_DEFAULT_TOPIC_PREFIX, DEFAULT_CLOUD_HOST, DEFAULT_CLOUD_PORT


def build_bridge_args(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = CLOUD_DEFAULT_TOPIC_PREFIX,
) -> dict[str, str | int]:
    """Build Bridge constructor keyword arguments from CLI/env parameters targeting Gateway."""
    args: dict[str, str | int] = {}
    effective_host = (
        host or os.environ.get("MCUBRIDGE_GATEWAY_HOST") or os.environ.get("MCUBRIDGE_CLOUD_HOST") or DEFAULT_CLOUD_HOST
    )
    raw_port = (
        port or os.environ.get("MCUBRIDGE_GATEWAY_PORT") or os.environ.get("MCUBRIDGE_CLOUD_PORT") or DEFAULT_CLOUD_PORT
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
