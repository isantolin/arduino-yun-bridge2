"""Lightweight MQTT enums and shared facilities."""
from __future__ import annotations

from enum import IntEnum

from aiomqtt import Client as MqttClient, MqttError

from .messages import QueuedPublish

__all__ = [
    "MqttError",
    "MQTTError",
    "MQTTClient",
    "QOSLevel",
    "QueuedPublish",
]


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


MQTTClient = MqttClient
MQTTError = MqttError

