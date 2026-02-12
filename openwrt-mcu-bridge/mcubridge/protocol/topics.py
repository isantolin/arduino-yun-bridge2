"""MQTT topic helpers shared across McuBridge components.

This module is the SINGLE SOURCE OF TRUTH for MQTT topic structures.
Avoid hardcoding topic strings elsewhere.
"""

from __future__ import annotations
import msgspec
from .protocol import Topic

class TopicRoute(msgspec.Struct, frozen=True):
    """Parsed representation of an MQTT topic targeting the daemon."""
    raw: str
    prefix: str
    topic: Topic
    segments: tuple[str, ...]

    @property
    def identifier(self) -> str:
        return self.segments[0] if self.segments else ""

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:] if len(self.segments) > 1 else ()


def _split_segments(path: str) -> tuple[str, ...]:
    if not path:
        return ()
    return tuple(segment for segment in path.split("/") if segment)


def topic_path(prefix: str, topic: Topic | str, *segments: str) -> str:
    """Join prefix, topic and optional sub-segments into a topic path."""
    parts = list(_split_segments(prefix))
    topic_segment = topic.value if isinstance(topic, Topic) else str(topic)
    topic_segment = topic_segment.strip("/")
    if not topic_segment:
        raise ValueError("topic segment cannot be empty")
    parts.append(topic_segment)
    for segment in segments:
        cleaned = segment.strip("/")
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts)


# --- Service Specific Topics ---


def pin_topic(prefix: str, pin: int | str, action: str = "read") -> str:
    """e.g. br/d/13/read"""
    return topic_path(prefix, Topic.DIGITAL, str(pin), action)


def analog_pin_topic(prefix: str, pin: int | str, action: str = "read") -> str:
    """e.g. br/a/0/read"""
    return topic_path(prefix, Topic.ANALOG, str(pin), action)


def datastore_topic(prefix: str, key: str, action: str = "get") -> str:
    """e.g. br/datastore/get/mykey"""
    return topic_path(prefix, Topic.DATASTORE, action, key)


def file_topic(prefix: str, action: str, filename: str) -> str:
    """e.g. br/file/read/etc/config/network"""
    return topic_path(prefix, Topic.FILE, action, filename)


def shell_topic(prefix: str, action: str, command_id: str | None = None) -> str:
    """e.g. br/sh/run or br/sh/poll/cmd123"""
    if command_id:
        return topic_path(prefix, Topic.SHELL, action, command_id)
    return topic_path(prefix, Topic.SHELL, action)


def handshake_topic(prefix: str) -> str:
    return topic_path(prefix, Topic.SYSTEM, "handshake")


def mailbox_incoming_available_topic(prefix: str) -> str:
    return topic_path(prefix, Topic.MAILBOX, "incoming_available")


def mailbox_outgoing_available_topic(prefix: str) -> str:
    return topic_path(prefix, Topic.MAILBOX, "outgoing_available")


def parse_topic(prefix: str, topic_name: str) -> TopicRoute | None:
    """Parse an incoming MQTT topic into a TopicRoute."""
    prefix_segments = _split_segments(prefix)
    topic_segments = _split_segments(topic_name)
    if len(topic_segments) < len(prefix_segments) + 1:
        return None
    if topic_segments[: len(prefix_segments)] != prefix_segments:
        return None
    topic_segment = topic_segments[len(prefix_segments)]
    try:
        topic_enum = Topic(topic_segment)
    except ValueError:
        return None
    remainder_start = len(prefix_segments) + 1
    remainder = topic_segments[remainder_start:]
    normalized_prefix = "/".join(prefix_segments)
    return TopicRoute(
        raw=topic_name,
        prefix=normalized_prefix,
        topic=topic_enum,
        segments=remainder,
    )
