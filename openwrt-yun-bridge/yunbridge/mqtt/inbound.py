"""Helper utilities for working with inbound aiomqtt messages."""

from __future__ import annotations

from typing import Any, cast
from collections.abc import Iterable

from aiomqtt.message import Message as MQTTMessage

__all__ = [
    "MQTTMessage",
    "topic_name",
    "response_topic",
    "correlation_data",
    "user_properties",
    "content_type",
    "message_expiry_interval",
    "payload_format_indicator",
    "topic_alias",
]


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "value", None)
    if isinstance(text, str):
        return text
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _properties(message: MQTTMessage) -> Any:
    return getattr(message, "properties", None)


def topic_name(message: MQTTMessage) -> str:
    """Return the MQTT topic as a normalized string."""

    topic = getattr(message, "topic", "")
    return _to_str(topic)


def response_topic(message: MQTTMessage) -> str | None:
    """Return the v5 response-topic value if present."""

    properties = _properties(message)
    if properties is None:
        return None
    response = getattr(properties, "ResponseTopic", None)
    if response is None:
        return None
    return _to_str(response) or None


def correlation_data(message: MQTTMessage) -> bytes | None:
    """Return correlation-data from the MQTT properties."""

    properties = _properties(message)
    if properties is None:
        return None
    data = getattr(properties, "CorrelationData", None)
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, memoryview):
        return data.tobytes()
    return None


def user_properties(message: MQTTMessage) -> tuple[tuple[str, str], ...]:
    """Return normalized user-properties from the MQTT message."""

    properties = _properties(message)
    if properties is None:
        return ()
    raw_props = getattr(properties, "UserProperty", None)
    if not raw_props:
        return ()
    normalized: list[tuple[str, str]] = []
    for entry in raw_props:
        if not isinstance(entry, Iterable) or isinstance(entry, (bytes, str)):
            continue
        iterable_entry = cast(Iterable[Any], entry)
        entry_items = tuple(str(part) for part in iterable_entry)
        if len(entry_items) >= 2:
            normalized.append((entry_items[0], entry_items[1]))
    return tuple(normalized)


def content_type(message: MQTTMessage) -> str | None:
    properties = _properties(message)
    if properties is None:
        return None
    value = getattr(properties, "ContentType", None)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def message_expiry_interval(message: MQTTMessage) -> int | None:
    properties = _properties(message)
    if properties is None:
        return None
    expiry = getattr(properties, "MessageExpiryInterval", None)
    try:
        return int(expiry) if expiry is not None else None
    except (TypeError, ValueError):
        return None


def payload_format_indicator(message: MQTTMessage) -> int | None:
    properties = _properties(message)
    if properties is None:
        return None
    indicator = getattr(properties, "PayloadFormatIndicator", None)
    try:
        return int(indicator) if indicator is not None else None
    except (TypeError, ValueError):
        return None


def topic_alias(message: MQTTMessage) -> int | None:
    properties = _properties(message)
    if properties is None:
        return None
    alias = getattr(properties, "TopicAlias", None)
    try:
        return int(alias) if alias is not None else None
    except (TypeError, ValueError):
        return None
