"""MQTT topic helpers shared across McuBridge components.

This module is the SINGLE SOURCE OF TRUTH for MQTT topic structures.
Avoid hardcoding topic strings elsewhere.
"""

from __future__ import annotations

from typing import Any

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
    def action(self) -> Any:
        """Infer the service action from the first segment if applicable.
        Ignore segments that indicate a response flavor.
        """
        from .protocol import FileAction, ShellAction, SystemAction

        if not self.segments or "response" in self.segments or "value" in self.segments:
            return None
        val = self.segments[0]
        # Attempt to map to known action enums
        for enum_cls in (FileAction, ShellAction, SystemAction):
            try:
                return enum_cls(val)
            except ValueError:
                continue
        return val

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:] if len(self.segments) > 1 else ()


def split_topic_segments(path: str) -> tuple[str, ...]:
    """Public helper for service-level topic segment normalization."""
    return tuple(filter(None, path.split("/")))


def topic_path(prefix: str, topic: Topic | str, *segments: str) -> str:
    """Join prefix, topic and optional sub-segments into a topic path."""
    parts = list(split_topic_segments(prefix))
    topic_segment = topic.value if isinstance(topic, Topic) else str(topic)
    topic_segment = topic_segment.strip("/")
    if topic_segment:
        parts.append(topic_segment)
    parts.extend(filter(None, (s.strip("/") for s in segments)))
    return "/".join(parts)


# --- Service Specific Topics ---


def parse_topic(prefix: str, topic_name: str) -> TopicRoute | None:
    """Parse an incoming MQTT topic into a TopicRoute."""
    prefix_segments = split_topic_segments(prefix)
    topic_segments = split_topic_segments(topic_name)
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
