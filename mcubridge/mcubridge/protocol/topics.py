"""MQTT topic helpers shared across McuBridge components.

This module is the SINGLE SOURCE OF TRUTH for MQTT topic structures.
Avoid hardcoding topic strings elsewhere.
"""

from __future__ import annotations


from .protocol import Topic
from .structures import TopicRoute


def topic_path(prefix: str, topic: Topic, *segments: str | int) -> str:
    """[SIL-2] Construct topic path using direct join/filter delegation."""
    # Eradicate manual part.append() loops in favor of a single generator.
    parts = (str(s).strip("/") for s in (prefix, topic, *segments))
    return "/".join(filter(None, parts))


# --- Service Specific Topics ---


def parse_topic(prefix: str, topic_name: str) -> TopicRoute | None:
    """Parse an incoming MQTT topic into a TopicRoute.
    Returns None if the topic does not match the prefix or is malformed.
    """
    if not topic_name or not prefix:
        return None

    # [SIL-2] Holistic topic decomposition using library-native filter/split.
    prefix_segs = tuple(filter(None, prefix.split("/")))
    topic_segs = tuple(filter(None, topic_name.split("/")))

    # Validation: must contain prefix + at least one service segment
    if (
        len(topic_segs) <= len(prefix_segs)
        or topic_segs[: len(prefix_segs)] != prefix_segs
    ):
        return None

    try:
        # Identify service topic and extract remainder segments
        topic_enum = Topic(topic_segs[len(prefix_segs)])
        return TopicRoute(
            raw=topic_name,
            prefix="/".join(prefix_segs),
            topic=topic_enum,
            segments=topic_segs[len(prefix_segs) + 1 :],
        )
    except ValueError:
        return None
