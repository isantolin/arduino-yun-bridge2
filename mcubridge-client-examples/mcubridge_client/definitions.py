from __future__ import annotations

import ssl
from enum import IntEnum

from mcubridge.config.const import DEFAULT_MQTT_PORT
from mcubridge.protocol.structures import QOSLevel, UserProperty, build_mqtt_properties
from mcubridge.protocol.mcubridge_pb2 import MqttQueuedPublish
from .protocol import MAX_PAYLOAD_SIZE

# Client-specific default (remote board IP, NOT localhost)
DEFAULT_MQTT_HOST: str = "192.168.15.36"
DEFAULT_MQTT_TOPIC: str = "br"


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
    user: str | None = None,
    password: str | None = None,
    tls_insecure: bool = False,
    *,
    disable_tls: bool = False,
) -> dict[str, object]:
    """Build Bridge constructor keyword arguments from CLI/env parameters."""
    args: dict[str, object] = {}
    if host:
        args["host"] = host
    if port:
        args["port"] = port
    if user:
        args["username"] = user
    if password:
        args["password"] = password
    if tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        args["tls_context"] = ctx
    elif disable_tls:
        args["tls_context"] = None
    return args


__all__ = [
    "DEFAULT_MQTT_HOST",
    "DEFAULT_MQTT_PORT",
    "DEFAULT_MQTT_TOPIC",
    "MAX_PAYLOAD_SIZE",
    "QOSLevel",
    "MqttQueuedPublish",
    "SpiBitOrder",
    "SpiMode",
    "UserProperty",
    "build_bridge_args",
    "build_mqtt_properties",
]
