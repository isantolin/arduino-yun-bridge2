"""MQTT helpers for McuBridge.

This module exposes property-builder utilities consumed by the MQTT
transport layer and by external test fixtures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

if TYPE_CHECKING:
    from mcubridge.mqtt.messages import QueuedPublish

__all__ = [
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
]


def build_mqtt_properties(message: QueuedPublish) -> Properties | None:
    """Construct Paho MQTT v5 properties from a message object."""
    has_props = any(
        [
            message.content_type,
            message.payload_format_indicator is not None,
            message.message_expiry_interval is not None,
            message.response_topic,
            message.correlation_data is not None,
            message.user_properties,
        ]
    )

    if not has_props:
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
    return props
