"""Property-based tests for CLOUD topics module using Hypothesis. [SIL-2]"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from mcubridge.protocol.protocol import COMMAND_TO_TOPIC, Topic
from mcubridge.protocol.topics import (
    get_topic_for_message,
    parse_topic,
    topic_path,
)


_SAFE_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=16,
)


@settings(max_examples=50, derandomize=True, deadline=None)
@given(
    prefix=_SAFE_TEXT,
    topic=st.sampled_from(list(Topic)),
    segments=st.lists(_SAFE_TEXT, max_size=4),
)
def test_topic_path_parse_topic_isomorphism(prefix: str, topic: Topic, segments: list[str]) -> None:
    """Property: Any valid (prefix, topic, *segments) tuple roundtrips through parse_topic idempotently."""
    raw = topic_path(prefix, topic.value, *segments)
    route = parse_topic(prefix, raw)
    assert route is not None
    assert route.raw == raw
    assert route.prefix == prefix
    assert route.topic == topic
    assert route.segments == tuple(segments)


@settings(max_examples=40, derandomize=True, deadline=None)
@given(
    prefix_a=_SAFE_TEXT,
    prefix_b=_SAFE_TEXT,
    topic=st.sampled_from(list(Topic)),
    segments=st.lists(_SAFE_TEXT, max_size=2),
)
def test_parse_topic_mismatched_prefix_rejected(
    prefix_a: str, prefix_b: str, topic: Topic, segments: list[str]
) -> None:
    """Property: A topic constructed with prefix_b is deterministically rejected when parsed with distinct prefix_a."""
    if prefix_a == prefix_b:
        return
    raw = topic_path(prefix_b, topic.value, *segments)
    assert parse_topic(prefix_a, raw) is None


@settings(max_examples=40, derandomize=True, deadline=None)
@given(
    prefix=_SAFE_TEXT,
    invalid_service=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=8, max_size=16).filter(
        lambda s: s not in {t.value for t in Topic} and s not in {"digital", "analog", "shell"}
    ),
    segments=st.lists(_SAFE_TEXT, max_size=2),
)
def test_parse_topic_unrecognized_service_rejected(prefix: str, invalid_service: str, segments: list[str]) -> None:
    """Property: Any service segment not recognized in Topic enum or aliases returns None."""
    raw = topic_path(prefix, invalid_service, *segments)
    assert parse_topic(prefix, raw) is None


@settings(max_examples=30, derandomize=True, deadline=None)
@given(s=_SAFE_TEXT)
def test_parse_topic_empty_inputs_rejected(s: str) -> None:
    """Property: Empty prefix or empty topic string always returns None."""
    assert parse_topic("", s) is None
    assert parse_topic(s, "") is None
    assert parse_topic("", "") is None


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    prefix=_SAFE_TEXT,
    command_id=st.sampled_from(list(COMMAND_TO_TOPIC.keys())),
)
def test_get_topic_for_message_command_id_known(prefix: str, command_id: int) -> None:
    """Property: get_topic_for_message returns exact topic path for all registered commands."""
    res = get_topic_for_message(prefix, command_id)
    assert res is not None
    assert res.startswith(prefix)
    assert COMMAND_TO_TOPIC[command_id] in res


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    prefix=_SAFE_TEXT,
    unknown_id=st.integers(min_value=60000, max_value=65535),
)
def test_get_topic_for_message_unknown_id(prefix: str, unknown_id: int) -> None:
    """Property: get_topic_for_message returns None for any unregistered command ID."""
    if unknown_id in COMMAND_TO_TOPIC:
        return
    assert get_topic_for_message(prefix, unknown_id) is None
