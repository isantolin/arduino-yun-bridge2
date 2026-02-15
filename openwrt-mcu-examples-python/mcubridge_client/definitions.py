from __future__ import annotations

import time
from enum import IntEnum
from typing import Final, TypedDict
import msgspec

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

# Constants
PROTOCOL_MAX_PAYLOAD_SIZE: Final[int] = 64  # Matches protocol.MAX_PAYLOAD_SIZE
DEFAULT_MQTT_HOST: str = "192.168.15.36"
DEFAULT_MQTT_PORT: int = 8883
DEFAULT_MQTT_TOPIC: str = "br"
MAX_PAYLOAD_SIZE: Final[int] = PROTOCOL_MAX_PAYLOAD_SIZE


class QOSLevel(IntEnum):
    """MQTT Quality of Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


UserProperty = tuple[str, str]


class SpoolRecord(TypedDict):
    """Schema for spooled MQTT messages on disk."""

    topic: str
    payload_base64: str
    qos: int
    retain: bool
    expiry: int | None
    user_properties: list[list[str]] | None
    timestamp: float


class QueuedPublish(msgspec.Struct, frozen=True):
    """Internal representation of a message waiting to be published."""

    topic_name: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    message_expiry_interval: int | None = None
    topic_alias: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: tuple[UserProperty, ...] = ()
    subscription_identifier: tuple[int, ...] | None = None
    payload_format_indicator: int | None = None  # 0=bytes, 1=utf-8

    def to_spool_record(self) -> SpoolRecord:
        """Convert to a dictionary suitable for disk spooling."""
        import base64

        return {
            "topic": self.topic_name,
            "payload_base64": base64.b64encode(self.payload).decode("ascii"),
            "qos": self.qos,
            "retain": self.retain,
            "expiry": self.message_expiry_interval,
            "user_properties": [list(p) for p in self.user_properties] if self.user_properties else None,
            "timestamp": time.time(),
        }


def build_mqtt_properties(message: QueuedPublish) -> Properties:
    """Construct MQTT 5.0 properties object for aiomqtt/paho."""
    props = Properties(PacketTypes.PUBLISH)

    if message.content_type is not None:
        props.ContentType = message.content_type

    if message.message_expiry_interval is not None:
        props.MessageExpiryInterval = int(message.message_expiry_interval)

    if message.topic_alias is not None:
        props.TopicAlias = message.topic_alias

    if message.response_topic is not None:
        props.ResponseTopic = message.response_topic

    if message.correlation_data is not None:
        props.CorrelationData = message.correlation_data

    if message.user_properties:
        props.UserProperty = list(message.user_properties)

    if message.subscription_identifier is not None:
        props.SubscriptionIdentifier = list(message.subscription_identifier)

    if message.payload_format_indicator is not None:
        props.PayloadFormatIndicator = message.payload_format_indicator

    return props


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
