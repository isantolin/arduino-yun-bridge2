from __future__ import annotations

import ssl
from enum import IntEnum
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from mcubridge.config.const import DEFAULT_MQTT_PORT
from mcubridge.protocol.structures import QOSLevel, UserProperty, QueuedPublish
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


def build_mqtt_properties(message: QueuedPublish) -> Properties:
    """Construct MQTT 5.0 properties object for aiomqtt/paho.

    Reuses the daemon's core property builder and extends it with
    client-specific fields (topic_alias, subscription_identifier).
    """
    # The daemon helper returns None when no standard fields are set;
    # the client always needs a Properties object for the extra fields.
    import mcubridge.mqtt

    props = mcubridge.mqtt.build_mqtt_properties(message) or Properties(PacketTypes.PUBLISH)

    # Client-specific MQTT v5 properties not used by the daemon
    if message.topic_alias is not None:
        props.TopicAlias = message.topic_alias

    if message.subscription_identifier is not None:
        props.SubscriptionIdentifier = list(message.subscription_identifier)

    return props


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
    "QueuedPublish",
    "SpiBitOrder",
    "SpiMode",
    "UserProperty",
    "build_bridge_args",
    "build_mqtt_properties",
]
