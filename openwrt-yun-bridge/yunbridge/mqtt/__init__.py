"""Lightweight MQTT type helpers backed by asyncio-mqtt."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Self

from .client import Client, MQTTError


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

    def with_payload(
        self,
        payload: bytes,
        *,
        qos: Optional[QOSLevel] = None,
        retain: Optional[bool] = None,
    ) -> Self:
        """Return a copy with updated payload/QoS/retain flags."""

        return PublishableMessage(
            topic_name=self.topic_name,
            payload=payload,
            qos=qos if qos is not None else self.qos,
            retain=retain if retain is not None else self.retain,
        )


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


__all__ = [
    "Client",
    "MQTTError",
    "PublishableMessage",
    "DeliveredMessage",
    "QOSLevel",
    "as_delivered_message",
]
