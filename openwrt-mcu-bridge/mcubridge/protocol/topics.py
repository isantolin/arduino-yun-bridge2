"""MQTT topic helpers shared across McuBridge components."""

from __future__ import annotations

from dataclasses import dataclass

from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import Topic


@dataclass(frozen=True)
class TopicRoute:
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


def topic_path(
    prefix: str,
    topic: Topic | str,
    *segments: str,
) -> str:
    """Join *prefix*, *topic* and optional sub-segments into a topic path."""

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


def parse_topic(prefix: str, topic_name: str) -> TopicRoute | None:
    """Parse an incoming MQTT topic into a :class:`TopicRoute`."""

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


_HANDSHAKE_SEGMENT = "handshake"


def handshake_topic(prefix: str) -> str:
    """Return the MQTT topic used for publishing handshake telemetry."""

    return topic_path(prefix, Topic.SYSTEM, _HANDSHAKE_SEGMENT)


def mailbox_incoming_available_topic(prefix: str) -> str:
    """Topic path that reports queued MCU->Linux mailbox messages."""

    return topic_path(
        prefix,
        Topic.MAILBOX,
        protocol.MQTT_SUFFIX_INCOMING_AVAILABLE,
    )


def mailbox_outgoing_available_topic(prefix: str) -> str:
    """Topic path that reports queued Linux->MCU mailbox messages."""

    return topic_path(
        prefix,
        Topic.MAILBOX,
        protocol.MQTT_SUFFIX_OUTGOING_AVAILABLE,
    )


__all__ = [
    "Topic",
    "TopicRoute",
    "handshake_topic",
    "mailbox_incoming_available_topic",
    "mailbox_outgoing_available_topic",
    "parse_topic",
    "topic_path",
]
