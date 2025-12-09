"""Utilities for building aiomqtt messages in tests."""
from __future__ import annotations

from aiomqtt.message import Message
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties


def make_inbound_message(
    topic: str,
    payload: bytes = b"",
    *,
    qos: int = 0,
    retain: bool = False,
    response_topic: str | None = None,
    correlation_data: bytes | None = None,
) -> Message:
    """Build a minimal aiomqtt Message with optional v5 metadata."""

    properties: Properties | None = None
    if response_topic is not None or correlation_data is not None:
        properties = Properties(PacketTypes.PUBLISH)
        if response_topic is not None:
            properties.ResponseTopic = response_topic
        if correlation_data is not None:
            properties.CorrelationData = correlation_data

    return Message(
        topic=topic,
        payload=payload,
        qos=qos,
        retain=retain,
        mid=1,
        properties=properties,
    )
