"""Lightweight MQTT type helpers and DTOs."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from aiomqtt import Client as MqttClient, MqttError

from .messages import QueuedPublish

# Re-export exceptions for convenience
__all__ = [
    "MqttError",
    "MQTTError",
    "QOSLevel",
    "QueuedPublish",
    "InboundMessage",
    "as_inbound_message",
    "MQTTClient",
]


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""
    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


MQTTClient = MqttClient
MQTTError = MqttError

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
