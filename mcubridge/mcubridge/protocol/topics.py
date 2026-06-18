"""MQTT topic helpers shared across McuBridge components.

This module is the SINGLE SOURCE OF TRUTH for MQTT topic structures.
Avoid hardcoding topic strings elsewhere.
"""

from __future__ import annotations

from .protocol import Topic
from .structures import TopicRoute
from google.protobuf.message import Message as ProtobufMessage


import posixpath
import functools


def topic_path(prefix: str, topic: str | Topic, *segments: str | int) -> str:
    """[SIL-2] Construct topic path using direct join/filter delegation."""
    # Eradicate manual part.append() loops in favor of a single generator.
    parts = [str(s).strip("/") for s in (prefix, topic, *segments) if str(s).strip("/")]
    return posixpath.join(*parts) if parts else ""


def get_topic_for_message(prefix: str, message: ProtobufMessage | type[ProtobufMessage] | int | str) -> str | None:
    """Resolve the canonical MQTT topic for a given message instance, class, command ID or enum name. [SIL-2]"""
    from .protocol import COMMAND_TO_TOPIC, MESSAGE_TO_TOPIC

    rel = None
    if isinstance(message, int):
        rel = COMMAND_TO_TOPIC.get(message)
    elif isinstance(message, str):
        rel = MESSAGE_TO_TOPIC.get(message)
    elif isinstance(message, type):
        rel = MESSAGE_TO_TOPIC.get(message)
    else:
        rel = MESSAGE_TO_TOPIC.get(type(message))

    if rel:
        return topic_path(prefix, *rel.split("/"))
    return None


# --- Service Specific Topics ---


@functools.lru_cache(maxsize=32)
def _get_prefix_segs(prefix: str) -> tuple[str, ...]:
    """Cache prefix segment splits. [SIL-2]"""
    return tuple(filter(None, prefix.split("/")))


@functools.lru_cache(maxsize=256)
def parse_topic(prefix: str, topic_name: str) -> TopicRoute | None:
    """Parse an incoming MQTT topic into a TopicRoute.
    Returns None if the topic does not match the prefix or is malformed.
    """
    if not topic_name or not prefix:
        return None

    # [SIL-2] Holistic topic decomposition using library-native filter/split.
    prefix_segs = _get_prefix_segs(prefix)
    topic_segs = tuple(filter(None, topic_name.split("/")))

    # Validation: must contain prefix + at least one service segment
    if len(topic_segs) <= len(prefix_segs) or topic_segs[: len(prefix_segs)] != prefix_segs:
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
