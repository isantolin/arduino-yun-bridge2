"""Lightweight MQTT type helpers and DTOs."""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any, Mapping, Sequence, Self, cast

from aiomqtt import MqttError

# Re-export exceptions for convenience
__all__ = [
    "MqttError",
    "QOSLevel",
    "PublishableMessage",
    "InboundMessage",
    "as_inbound_message",
]


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
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: tuple[tuple[str, str], ...] = ()

    def with_payload(
        self,
        payload: bytes,
        *,
        qos: QOSLevel | None = None,
        retain: bool | None = None,
        content_type: str | None = None,
        payload_format_indicator: int | None = None,
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

    def with_correlation_data(self, data: bytes | None) -> Self:
        return replace(self, correlation_data=data)

    def with_response_topic(self, topic: str | None) -> Self:
        return replace(self, response_topic=topic)

    def with_user_property(self, key: str, value: str) -> Self:
        return replace(
            self,
            user_properties=self.user_properties + ((key, value),),
        )

    def with_user_properties(
        self,
        properties: Mapping[object, object]
        | Sequence[tuple[Any, Any]],
    ) -> Self:
        pairs: Sequence[tuple[Any, Any]]
        if isinstance(properties, Mapping):
            pairs = tuple(properties.items())
        else:
            pairs = properties
        items = tuple((str(k), str(v)) for k, v in pairs)
        return replace(self, user_properties=self.user_properties + items)

    def with_message_expiry(self, ttl_seconds: int | None) -> Self:
        return replace(self, message_expiry_interval=ttl_seconds)

    def with_content_type(self, content_type: str | None) -> Self:
        return replace(self, content_type=content_type)

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
        payload_b64 = str(record.get("payload", ""))
        payload = base64.b64decode(payload_b64.encode("ascii"))

        correlation_raw = record.get("correlation_data")
        correlation_data = None
        if correlation_raw is not None:
            encoded = str(correlation_raw).encode("ascii")
            correlation_data = base64.b64decode(encoded)

        raw_properties = record.get("user_properties")
        user_properties: list[tuple[str, str]] = []
        if isinstance(raw_properties, (list, tuple)):
            for raw_entry_obj in raw_properties:
                if not isinstance(raw_entry_obj, (list, tuple)):
                    continue
                entry_seq = cast(Sequence[Any], raw_entry_obj)
                if len(entry_seq) < 2:
                    continue
                user_properties.append(
                    (str(entry_seq[0]), str(entry_seq[1]))
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
            user_properties=tuple(user_properties),
        )


@dataclass(slots=True)
class InboundMessage:
    """Rich representation of an incoming MQTT message with v5 metadata."""

    topic_name: str
    payload: bytes
    qos: QOSLevel
    retain: bool
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: tuple[tuple[str, str], ...] = ()
    content_type: str | None = None
    message_expiry_interval: int | None = None
    payload_format_indicator: int | None = None
    topic_alias: int | None = None


def as_inbound_message(raw_message: Any) -> InboundMessage:
    """Convert an aiomqtt message into InboundMessage."""
    topic_obj = getattr(raw_message, "topic", "")
    topic = str(topic_obj) if topic_obj is not None else ""
    payload = getattr(raw_message, "payload", None) or b""

    qos_val = getattr(raw_message, "qos", 0)
    try:
        qos = QOSLevel(int(qos_val))
    except (ValueError, TypeError):
        qos = QOSLevel.QOS_0

    retain = bool(getattr(raw_message, "retain", False))

    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: list[tuple[str, str]] = []
    content_type: str | None = None
    message_expiry: int | None = None
    payload_format_indicator: int | None = None
    topic_alias: int | None = None

    # aiomqtt exposes properties via .properties (if paho < 2) or direct
    # attributes when running with the modern paho 2.x stack.
    properties = getattr(raw_message, "properties", None)

    if properties is not None:
        response_topic = getattr(properties, "ResponseTopic", None)
        # Some paho versions return bytes for string props, normalize
        if isinstance(response_topic, bytes):
            response_topic = response_topic.decode("utf-8", errors="ignore")

        correlation_data = getattr(properties, "CorrelationData", None)

        raw_user_props = getattr(properties, "UserProperty", None)
        if raw_user_props:
            for k, v in raw_user_props:
                user_properties.append((str(k), str(v)))

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
        user_properties=tuple(user_properties),
        content_type=content_type,
        message_expiry_interval=message_expiry,
        payload_format_indicator=payload_format_indicator,
        topic_alias=topic_alias,
    )
