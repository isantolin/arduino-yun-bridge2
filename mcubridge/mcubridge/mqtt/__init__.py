"""MQTT helpers for McuBridge.

This module exposes property-builder utilities consumed by the MQTT
transport layer and by external test fixtures.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from mcubridge.protocol import protocol


class MQTTPublishable(Protocol):
    """Structural type for objects that carry MQTT v5 publish properties."""

    @property
    def content_type(self) -> str | None: ...

    @property
    def payload_format_indicator(self) -> int | None: ...

    @property
    def message_expiry_interval(self) -> int | None: ...

    @property
    def response_topic(self) -> str | None: ...

    @property
    def correlation_data(self) -> bytes | None: ...

    @property
    def user_properties(self) -> Sequence[tuple[str, str]]: ...


__all__ = [
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
    "MQTTPublishable",
]


def build_mqtt_properties(message: MQTTPublishable) -> Properties | None:
    """Construct Paho MQTT v5 properties from a message object."""
    if not any(
        (
            message.content_type,
            message.payload_format_indicator is not None,
            message.message_expiry_interval is not None,
            message.response_topic,
            message.correlation_data is not None,
            message.user_properties,
        )
    ):
        return None

    props = Properties(PacketTypes.PUBLISH)

    if message.content_type is not None:
        props.ContentType = message.content_type

    if message.payload_format_indicator is not None:
        props.PayloadFormatIndicator = message.payload_format_indicator

    if message.message_expiry_interval is not None:
        props.MessageExpiryInterval = int(message.message_expiry_interval)

    if message.response_topic:
        props.ResponseTopic = message.response_topic

    if message.correlation_data is not None:
        props.CorrelationData = message.correlation_data

    if message.user_properties:
        props.UserProperty = list(message.user_properties)

    return props


def build_mqtt_connect_properties() -> Properties:
    """Return default CONNECT properties for aiomqtt/paho clients."""

    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = 0
    props.RequestResponseInformation = 1
    props.RequestProblemInformation = 1
    # [OPTIMIZATION] Enable Topic Aliases (MQTT v5) to reduce overhead
    props.TopicAliasMaximum = protocol.MAX_PAYLOAD_SIZE
    return props
