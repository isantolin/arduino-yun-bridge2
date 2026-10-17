"""CLOUD topic helpers shared across McuBridge components.

This module is the SINGLE SOURCE OF TRUTH for CLOUD topic structures.
Avoid hardcoding topic strings elsewhere.
"""

from __future__ import annotations

import functools
import posixpath
from dataclasses import dataclass
from typing import Any, Final

import parse
import structlog
from google.protobuf.message import Message as ProtobufMessage

from .protocol import REGISTRY, Topic

logger = structlog.get_logger(__name__)

_FLAVOR_SEGMENTS: Final[frozenset[str]] = frozenset({"response", "value"})


@dataclass(slots=True, frozen=True)
class TopicRoute:
    """Parsed representation of a CLOUD topic targeting the daemon. [SIL-2]"""

    raw: str = ""
    prefix: str = ""
    topic: Topic | str = ""
    segments: tuple[str, ...] = ()

    @property
    def identifier(self) -> str:
        return self.segments[0] if self.segments else ""

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:]

    @property
    def action(self) -> str | None:
        """Infer the canonical action for authorization and dispatch. [SIL-2]"""
        if not self.segments or not _FLAVOR_SEGMENTS.isdisjoint(self.segments):
            return None
        if self.topic == Topic.SYSTEM or self.topic == "system":
            return None
        if self.topic in (Topic.DIGITAL, Topic.ANALOG, "digital", "analog", "d", "a"):
            return (
                "write" if len(self.segments) == 1 else (self.segments[1].lower() if len(self.segments) > 1 else None)
            )
        if self.topic in (Topic.CONSOLE, "console"):
            return "in" if self.identifier == "in" else (self.identifier or None)
        return self.identifier or None


def topic_path(prefix: str, topic: str | Topic, *segments: str | int) -> str:
    """[SIL-2] Construct topic path using direct join/filter delegation."""
    parts = [clean for s in (prefix, topic, *segments) if (clean := str(s).strip("/"))]
    return posixpath.join(*parts) if parts else ""


def get_topic_for_message(prefix: str, message: ProtobufMessage | type[ProtobufMessage] | int | str) -> str | None:
    """Resolve the canonical CLOUD topic for a given message instance, class, command ID or enum name. [SIL-2]"""
    spec = REGISTRY.get(message)
    if spec and spec.cloud_topic:
        return topic_path(prefix, *spec.cloud_topic.split("/"))
    return None


# --- Service Specific Topics ---


@functools.cache
def _get_prefix_segs(prefix: str) -> tuple[str, ...]:
    """Cache prefix segment splits. [SIL-2]"""
    return tuple(filter(None, prefix.split("/")))


@functools.lru_cache(maxsize=32)
def _get_topic_parser(prefix_canonical: str) -> tuple[parse.Parser, parse.Parser]:
    """Compile declarative topic parsers for prefix with and without remainder segments. [SIL-2]"""
    return (
        parse.compile(f"{prefix_canonical}/{{service}}/{{remainder}}"),
        parse.compile(f"{prefix_canonical}/{{service}}"),
    )


@functools.lru_cache(maxsize=256)
def parse_topic(prefix: str, topic_name: str) -> TopicRoute | None:
    """Parse an incoming CLOUD topic into a TopicRoute using declarative parse patterns. [SIL-2]"""
    if not topic_name or not prefix:
        return None

    prefix_segs = _get_prefix_segs(prefix)
    prefix_canonical = "/".join(prefix_segs)
    clean_topic = "/".join(filter(None, topic_name.split("/")))
    if not prefix_canonical or not clean_topic:
        return None

    full_parser, svc_parser = _get_topic_parser(prefix_canonical)
    parsed = full_parser.parse(clean_topic)
    if parsed is not None:
        seg = str(parsed["service"]).lower()
        remainder = str(parsed["remainder"])
        segments = tuple(filter(None, remainder.split("/")))
    else:
        parsed_svc = svc_parser.parse(clean_topic)
        if parsed_svc is None:
            return None
        seg = str(parsed_svc["service"]).lower()
        segments = ()

    topic_enum = REGISTRY.lookup_topic(seg)
    if topic_enum is None:
        logger.warning("Unrecognized cloud topic service segment", segment=seg, topic=topic_name)
        return None

    return TopicRoute(
        raw=topic_name,
        prefix=prefix_canonical,
        topic=topic_enum,
        segments=segments,
    )


@functools.lru_cache(maxsize=64)
def _get_subtopic_parser(format_spec: str) -> parse.Parser:
    """Compile and cache subtopic format parser for structured path segments. [SIL-2]"""
    return parse.compile(format_spec)


def parse_subtopic(path: str, format_spec: str) -> dict[str, Any] | None:
    """Extract structured named fields from subtopic remainder using declarative parse pattern. [SIL-2]"""
    clean_path = path.strip("/")
    result = _get_subtopic_parser(format_spec).parse(clean_path)
    return dict(result.named) if result is not None else None
