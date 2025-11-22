"""Lightweight MQTT type helpers backed by aiomqtt."""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import (
    Any,
    AsyncIterator,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Self,
    Tuple,
    cast,
)

from .client import Client, MQTTError


class ProtocolVersion(IntEnum):
    """MQTT protocol versions supported by the client wrapper."""

    V31 = 0x03
    V311 = 0x04
    V5 = 0x05


try:  # pragma: no cover - optional dependency path
    from paho.mqtt.packettypes import PacketTypes as _RuntimePacketTypes
    from paho.mqtt.properties import Properties as _RuntimeProperties
except Exception:  # pragma: no cover - allow tests without paho
    _RuntimePacketTypes = None
    _RuntimeProperties = None

PahoPacketTypes: Any = _RuntimePacketTypes
PahoProperties: Any = _RuntimeProperties


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
        properties: Optional[Any] = None,
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
    content_type: Optional[str] = None
    payload_format_indicator: Optional[int] = None
    message_expiry_interval: Optional[int] = None
    response_topic: Optional[str] = None
    correlation_data: Optional[bytes] = None
    user_properties: Tuple[Tuple[str, str], ...] = ()

    def with_payload(
        self,
        payload: bytes,
        *,
        qos: Optional[QOSLevel] = None,
        retain: Optional[bool] = None,
        content_type: Optional[str] = None,
        payload_format_indicator: Optional[int] = None,
    ) -> Self:
        """Return a copy with updated payload and optional metadata."""

        updated = replace(
            self,
            payload=payload,
            qos=qos if qos is not None else self.qos,
            retain=retain if retain is not None else self.retain,
        )
        if content_type is not None:
            updated = replace(updated, content_type=content_type)
        if payload_format_indicator is not None:
            updated = replace(
                updated,
                payload_format_indicator=payload_format_indicator,
            )
        return updated

    def with_topic(self, topic: str) -> Self:
        return replace(self, topic_name=topic)

    def with_correlation_data(self, data: Optional[bytes]) -> Self:
        return replace(self, correlation_data=data)

    def with_response_topic(self, topic: Optional[str]) -> Self:
        return replace(self, response_topic=topic)

    def with_user_property(self, key: str, value: str) -> Self:
        return replace(
            self,
            user_properties=self.user_properties + ((key, value),),
        )

    def with_user_properties(
        self,
        properties: Mapping[object, object]
        | Iterable[tuple[Any, Any]],
    ) -> Self:
        pairs: Iterable[tuple[Any, Any]]
        if isinstance(properties, Mapping):
            pairs = cast(Iterable[tuple[Any, Any]], properties.items())
        else:
            pairs = properties
        items = tuple((str(k), str(v)) for k, v in pairs)
        return replace(self, user_properties=self.user_properties + items)

    def with_message_expiry(self, ttl_seconds: Optional[int]) -> Self:
        return replace(self, message_expiry_interval=ttl_seconds)

    def with_content_type(self, content_type: Optional[str]) -> Self:
        return replace(self, content_type=content_type)

    def build_properties(self) -> Optional[Any]:
        if PahoProperties is None or PahoPacketTypes is None:
            return None

        if not any(
            [
                self.content_type,
                self.payload_format_indicator is not None,
                self.message_expiry_interval is not None,
                self.response_topic,
                self.correlation_data is not None,
                self.user_properties,
            ]
        ):
            return None

        props = PahoProperties(PahoPacketTypes.PUBLISH)
        if self.content_type is not None:
            props.content_type = self.content_type
        if self.payload_format_indicator is not None:
            props.payload_format_indicator = self.payload_format_indicator
        if self.message_expiry_interval is not None:
            props.message_expiry_interval = int(self.message_expiry_interval)
        if self.response_topic:
            props.response_topic = self.response_topic
        if self.correlation_data is not None:
            props.correlation_data = self.correlation_data
        if self.user_properties:
            props.user_property = list(self.user_properties)
        return props

    def to_spool_record(self) -> dict[str, Any]:
        """Serialize the message to a JSON-friendly mapping."""

        return {
            "topic_name": self.topic_name,
            "payload": base64.b64encode(self.payload).decode("ascii"),
            "qos": int(self.qos),
            "retain": self.retain,
            "content_type": self.content_type,
            "payload_format_indicator": self.payload_format_indicator,
            "message_expiry_interval": self.message_expiry_interval,
            "response_topic": self.response_topic,
            "correlation_data": (
                base64.b64encode(self.correlation_data).decode("ascii")
                if self.correlation_data is not None
                else None
            ),
            "user_properties": list(self.user_properties),
        }

    @classmethod
    def from_spool_record(
        cls,
        record: Mapping[str, Any],
    ) -> "PublishableMessage":
        payload_b64 = record.get("payload", "")
        payload = base64.b64decode(payload_b64.encode("ascii"))
        correlation_b64 = record.get("correlation_data")
        correlation_data = (
            base64.b64decode(str(correlation_b64).encode("ascii"))
            if correlation_b64 is not None
            else None
        )
        user_properties_raw = record.get("user_properties") or []
        user_properties = tuple(
            (str(key), str(value))
            for key, value in user_properties_raw
        )
        return cls(
            topic_name=str(record.get("topic_name", "")),
            payload=payload,
            qos=QOSLevel(int(record.get("qos", 0))),
            retain=bool(record.get("retain", False)),
            content_type=record.get("content_type"),
            payload_format_indicator=record.get("payload_format_indicator"),
            message_expiry_interval=record.get("message_expiry_interval"),
            response_topic=record.get("response_topic"),
            correlation_data=correlation_data,
            user_properties=user_properties,
        )


@dataclass(slots=True)
class DeliveredMessage:
    """Simplified representation of an incoming MQTT message."""

    topic_name: str
    payload: bytes
    qos: QOSLevel
    retain: bool


@dataclass(slots=True)
class InboundMessage:
    """Rich representation of an incoming MQTT message for the daemon."""

    topic_name: str
    payload: bytes
    qos: QOSLevel
    retain: bool
    response_topic: Optional[str] = None
    correlation_data: Optional[bytes] = None
    user_properties: Tuple[Tuple[str, str], ...] = ()
    content_type: Optional[str] = None
    message_expiry_interval: Optional[int] = None
    payload_format_indicator: Optional[int] = None
    topic_alias: Optional[int] = None


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


def as_inbound_message(raw_message: Any) -> InboundMessage:
    """Convert an aiomqtt message into InboundMessage with MQTT v5 metadata."""

    topic_obj = getattr(raw_message, "topic", "")
    topic = str(topic_obj) if topic_obj is not None else ""
    payload = getattr(raw_message, "payload", None) or b""
    qos_value = getattr(raw_message, "qos", 0)
    try:
        qos = QOSLevel(int(qos_value))
    except (ValueError, TypeError):
        qos = QOSLevel.QOS_0
    retain = bool(getattr(raw_message, "retain", False))

    response_topic: Optional[str] = None
    correlation_data: Optional[bytes] = None
    user_properties: Tuple[Tuple[str, str], ...] = ()
    content_type: Optional[str] = None
    message_expiry: Optional[int] = None
    payload_format_indicator: Optional[int] = None
    topic_alias: Optional[int] = None

    properties = getattr(raw_message, "properties", None)
    if properties is not None:
        response_topic = getattr(properties, "ResponseTopic", None)
        if isinstance(response_topic, bytes):
            response_topic = response_topic.decode("utf-8", errors="ignore")
        correlation_data = getattr(properties, "CorrelationData", None)
        user_property = getattr(properties, "UserProperty", None)
        if user_property:
            user_properties = tuple(
                (str(key), str(value)) for key, value in user_property
            )
        content_type = getattr(properties, "ContentType", None)
        message_expiry = getattr(properties, "MessageExpiryInterval", None)
        payload_format_indicator = getattr(
            properties,
            "PayloadFormatIndicator",
            None,
        )
        topic_alias = getattr(properties, "TopicAlias", None)

    return InboundMessage(
        topic_name=topic,
        payload=payload,
        qos=qos,
        retain=retain,
        response_topic=response_topic,
        correlation_data=correlation_data,
        user_properties=user_properties,
        content_type=content_type,
        message_expiry_interval=message_expiry,
        payload_format_indicator=payload_format_indicator,
        topic_alias=topic_alias,
    )


__all__ = [
    "Client",
    "MQTTError",
    "PublishableMessage",
    "DeliveredMessage",
    "InboundMessage",
    "QOSLevel",
    "ProtocolVersion",
    "MQTTClientProtocol",
    "MQTTIncomingMessage",
    "MQTTMessageStream",
    "as_delivered_message",
    "as_inbound_message",
]
