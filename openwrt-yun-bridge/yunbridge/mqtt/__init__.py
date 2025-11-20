"""Lightweight MQTT type helpers backed by aiomqtt."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, AsyncIterator, Optional, Protocol, Self

from .client import Client, MQTTError

try:  # pragma: no cover - optional dependency path
    from aiomqtt.client import ProtocolVersion  # type: ignore[import]
except Exception:  # pragma: no cover - testing fallback

    class ProtocolVersion(IntEnum):
        """Fallback protocol version enumerations."""

        V31 = 0x03
        V311 = 0x04
        V5 = 0x05


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


class MQTTIncomingMessage(Protocol):
    topic: Optional[str]
    payload: Optional[bytes]


class MQTTMessageStream(Protocol):
    async def __aenter__(self) -> AsyncIterator[MQTTIncomingMessage]:
        ...

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[Any],
    ) -> None:
        ...


class MQTTClientProtocol(Protocol):
    async def connect(self) -> None:
        ...

    async def disconnect(self) -> None:
        ...

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
    ) -> None:
        ...

    async def subscribe(self, topic: str, qos: int) -> None:
        ...

    def unfiltered_messages(self) -> MQTTMessageStream:
        ...


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

        return type(self)(
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
    """Convert raw aiomqtt message attributes into DeliveredMessage."""

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
    "ProtocolVersion",
    "MQTTClientProtocol",
    "MQTTIncomingMessage",
    "MQTTMessageStream",
    "as_delivered_message",
]
