"""Lightweight MQTT type helpers backed by asyncio-mqtt."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from asyncio_mqtt.error import MqttError

from .client import Client


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


@dataclass(slots=True)
class PublishableMessage:
    """Envelope describing an outgoing MQTT publication."""

    topic_name: str
    payload: bytes
    qos: QOSLevel = QOSLevel.QOS_0
    retain: bool = False


@dataclass(slots=True)
class DeliveredMessage:
    """Simplified representation of an incoming MQTT message."""

    topic_name: str
    payload: bytes
    qos: QOSLevel
    retain: bool


def as_delivered_message(
    *,
    topic: str,
    payload: Optional[bytes],
    qos: int,
    retain: bool,
) -> DeliveredMessage:
    """Convert raw asyncio-mqtt message attributes into DeliveredMessage."""

    normalized_qos = QOSLevel(qos) if qos in (0, 1, 2) else QOSLevel.QOS_0
    return DeliveredMessage(
        topic_name=topic,
        payload=payload or b"",
        qos=normalized_qos,
        retain=retain,
    )


MQTTError = MqttError

__all__ = [
    "Client",
    "MQTTError",
    "PublishableMessage",
    "DeliveredMessage",
    "QOSLevel",
    "as_delivered_message",
]
