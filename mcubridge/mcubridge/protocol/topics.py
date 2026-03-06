"""MQTT topic definitions and routing for the MCU Bridge service."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

logger = logging.getLogger("mcubridge.topics")

# Default topic prefix used across the ecosystem
MQTT_DEFAULT_TOPIC_PREFIX: Final[str] = "br"

# MQTT Wildcards
MQTT_WILDCARD_SINGLE: Final[str] = "+"
MQTT_WILDCARD_MULTI: Final[str] = "#"

# Response suffixes
MQTT_SUFFIX_RESPONSE: Final[str] = "response"


class Topic(StrEnum):
    """Root MQTT topics supported by the bridge."""

    SYSTEM = "system"
    DIGITAL = "d"
    ANALOG = "a"
    CONSOLE = "console"
    DATASTORE = "datastore"
    MAILBOX = "mailbox"
    FILE = "file"
    SHELL = "sh"
    STATUS = "status"
    METRICS = "metrics"


@dataclass(frozen=True)
class TopicRoute:
    """Represents a parsed MQTT topic path."""

    topic: Topic
    segments: Sequence[str]
    identifier: str = ""
    remainder: Sequence[str] = ()


def split_topic_segments(topic: str) -> list[str]:
    """Split an MQTT topic into its constituent segments."""
    return [s for s in topic.split("/") if s]


def parse_topic(topic: str) -> TopicRoute | None:
    """Parse an MQTT topic string into a TopicRoute."""
    segments = split_topic_segments(topic)
    if not segments:
        return None

    try:
        # Check if first segment is the prefix
        start_idx = 0
        if segments[0] == MQTT_DEFAULT_TOPIC_PREFIX:
            start_idx = 1

        if len(segments) <= start_idx:
            return None

        root = Topic(segments[start_idx])
        return TopicRoute(
            topic=root,
            segments=segments[start_idx:],
            identifier=segments[start_idx + 1] if len(segments) > start_idx + 1 else "",
            remainder=segments[start_idx + 2:] if len(segments) > start_idx + 2 else (),
        )
    except ValueError:
        return None


# For backward compatibility
topic_path = parse_topic

# Core command subscriptions for the daemon
MQTT_COMMAND_SUBSCRIPTIONS: Final[list[tuple[str, int]]] = [
    (f"{MQTT_DEFAULT_TOPIC_PREFIX}/{t.value}/{MQTT_WILDCARD_MULTI}", 1)
    for t in Topic
    if t not in (Topic.STATUS, Topic.METRICS)
]
