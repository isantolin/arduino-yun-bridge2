"""CLOUD topic helpers shared across McuBridge components.

This module is the SINGLE SOURCE OF TRUTH for CLOUD topic structures.
Avoid hardcoding topic strings elsewhere.
"""

from __future__ import annotations

import functools
import posixpath
from typing import Any

import parse
import structlog
from google.protobuf.message import Message as ProtobufMessage

from .protocol import COMMAND_TO_TOPIC, MESSAGE_TO_TOPIC, TOPIC_ALIASES, Topic
from .structures import TopicRoute

logger = structlog.get_logger(__name__)


def topic_path(prefix: str, topic: str | Topic, *segments: str | int) -> str:
    """[SIL-2] Construct topic path using direct join/filter delegation."""
    parts = [clean for s in (prefix, topic, *segments) if (clean := str(s).strip("/"))]
    return posixpath.join(*parts) if parts else ""


def get_topic_for_message(prefix: str, message: ProtobufMessage | type[ProtobufMessage] | int | str) -> str | None:
    """Resolve the canonical CLOUD topic for a given message instance, class, command ID or enum name. [SIL-2]"""
    if isinstance(message, int):
        rel = COMMAND_TO_TOPIC.get(message)
    else:
        key = getattr(message, "DESCRIPTOR", message)
        rel = MESSAGE_TO_TOPIC.get(key) or MESSAGE_TO_TOPIC.get(message)

    if rel:
        return topic_path(prefix, *rel.split("/"))
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

    topic_enum = TOPIC_ALIASES.get(seg)
    if topic_enum is None:
        try:
            topic_enum = Topic(seg)
        except ValueError as exc:
            logger.warning("Unrecognized cloud topic service segment", segment=seg, topic=topic_name, error=str(exc))
            return None

    return TopicRoute(
        raw=topic_name,
        prefix=prefix_canonical,
        topic=topic_enum,
        segments=segments,
    )


@functools.lru_cache(maxsize=64)
def compile_subtopic_parser(format_spec: str) -> parse.Parser:
    """Compile and cache subtopic format parser for structured path segments. [SIL-2]"""
    return parse.compile(format_spec)


def parse_subtopic(path: str, format_spec: str) -> dict[str, Any] | None:
    """Extract structured named fields from subtopic remainder using declarative parse pattern. [SIL-2]"""
    clean_path = path.strip("/")
    result = compile_subtopic_parser(format_spec).parse(clean_path)
    return dict(result.named) if result is not None else None

