from __future__ import annotations

from typing import Final

from mcubridge.mqtt import build_mqtt_properties
from mcubridge.mqtt.messages import QOSLevel, QueuedPublish, SpoolRecord, UserProperty
from mcubridge.protocol.protocol import MAX_PAYLOAD_SIZE as PROTOCOL_MAX_PAYLOAD_SIZE

# Constants
DEFAULT_MQTT_HOST: str = "192.168.15.36"
DEFAULT_MQTT_PORT: int = 8883
DEFAULT_MQTT_TOPIC: str = "br"
MAX_PAYLOAD_SIZE: Final[int] = PROTOCOL_MAX_PAYLOAD_SIZE

__all__ = [
    "DEFAULT_MQTT_HOST",
    "DEFAULT_MQTT_PORT",
    "DEFAULT_MQTT_TOPIC",
    "MAX_PAYLOAD_SIZE",
    "QOSLevel",
    "QueuedPublish",
    "SpoolRecord",
    "UserProperty",
    "build_mqtt_properties",
]
