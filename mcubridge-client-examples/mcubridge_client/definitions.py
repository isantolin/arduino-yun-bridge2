from __future__ import annotations

import ssl
import time
from enum import IntEnum
from typing import Final, TypedDict

import msgspec
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from mcubridge.config.const import DEFAULT_MQTT_PORT
from .protocol import MAX_PAYLOAD_SIZE as PROTOCOL_MAX_PAYLOAD_SIZE

# Client-specific default (remote board IP, NOT localhost)
DEFAULT_MQTT_HOST: str = "192.168.15.36"
MAX_PAYLOAD_SIZE: Final[int] = PROTOCOL_MAX_PAYLOAD_SIZE
DEFAULT_MQTT_TOPIC: str = "br"


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
            "user_properties": ([list(p) for p in self.user_properties] if self.user_properties else None),
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


def build_bridge_args(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    tls_insecure: bool = False,
    *,
    disable_tls: bool = False,
) -> dict[str, object]:
    """Build Bridge constructor keyword arguments from CLI/env parameters."""
    args: dict[str, object] = {}
    if host:
        args["host"] = host
    if port:
        args["port"] = port
    if user:
        args["username"] = user
    if password:
        args["password"] = password
    if tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        args["tls_context"] = ctx
    elif disable_tls:
        args["tls_context"] = None
    return args


__all__ = [
    "DEFAULT_MQTT_HOST",
    "DEFAULT_MQTT_PORT",
    "DEFAULT_MQTT_TOPIC",
    "MAX_PAYLOAD_SIZE",
    "QOSLevel",
    "QueuedPublish",
    "SpoolRecord",
    "UserProperty",
    "build_bridge_args",
    "build_mqtt_properties",
]
