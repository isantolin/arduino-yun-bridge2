"""Legacy shim that now defers to asyncio-mqtt's Client implementation."""
from __future__ import annotations

from asyncio_mqtt import Client
from asyncio_mqtt.error import MqttError

from . import DeliveredMessage, PublishableMessage, QOSLevel, as_delivered_message

MQTTError = MqttError

__all__ = [
    "Client",
    "MQTTError",
    "PublishableMessage",
    "DeliveredMessage",
    "QOSLevel",
    "as_delivered_message",
]
