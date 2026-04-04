"""MQTT topic helpers shared across McuBridge components.

This module is the SINGLE SOURCE OF TRUTH for MQTT topic structures.
Avoid hardcoding topic strings elsewhere.
"""

from __future__ import annotations


from .protocol import Topic, TopicBuilder
from .structures import TopicRoute


def split_topic_segments(path: str) -> tuple[str, ...]:
    """Public helper for service-level topic segment normalization."""
    return tuple(filter(None, path.split("/")))


def topic_path(prefix: str, topic: Topic | str, *segments: str | int) -> str:
    """Join prefix, topic and optional sub-segments into a topic path."""
    if isinstance(topic, Topic):
        return str(topic.build(prefix, *segments))
    return str(TopicBuilder(prefix, topic).add(*segments))


# --- Service Specific Topics ---


def parse_topic(prefix: str, topic_name: str) -> TopicRoute | None:
    """Parse an incoming MQTT topic into a TopicRoute.
    Returns None if the topic does not match the prefix or is malformed.
    """
    if not topic_name or not prefix:
        return None

    prefix_segments = split_topic_segments(prefix)
    topic_segments = split_topic_segments(topic_name)

    # Topic must have at least all prefix segments plus one for the service topic
    if len(topic_segments) < len(prefix_segments) + 1:
        return None

    # Prefix match check
    if topic_segments[: len(prefix_segments)] != prefix_segments:
        return None

    # Identify the service topic (e.g. 'd', 'a', 'sh')
    topic_segment = topic_segments[len(prefix_segments)]
    try:
        topic_enum = Topic(topic_segment)
    except ValueError:
        # Unknown service topic
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
