"""Property-based and unit tests for security policy objects. [SIL-2]"""

from __future__ import annotations

from hypothesis import given, strategies as st
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.structures import allows_topic, create_allowed_policy, is_command_allowed

_SAFE_TOKEN = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12)
_ARGS_STR = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_- ", max_size=10)


def _make_auth(**kwargs: bool) -> pb.TopicAuthorization:
    auth = pb.TopicAuthorization()
    for f in auth.DESCRIPTOR.fields:
        setattr(auth, f.name, kwargs.get(f.name, True))
    return auth


class TestAllowedCommandPolicy:
    @given(allowed=st.lists(_SAFE_TOKEN, min_size=1, max_size=5), args=_ARGS_STR)
    def test_allowed_commands_matched_by_first_token(self, allowed: list[str], args: str) -> None:
        """Property: A command is allowed if its first whitespace token matches any normalized policy entry."""
        policy = create_allowed_policy(allowed)
        cmd = f"{allowed[0]} {args}".strip()
        assert is_command_allowed(policy, cmd)

    @given(allowed=st.lists(_SAFE_TOKEN, min_size=1, max_size=5), cmd=st.text(alphabet=" \t\r\n", max_size=10))
    def test_empty_or_whitespace_always_denied(self, allowed: list[str], cmd: str) -> None:
        """Property: Empty or pure whitespace commands are always rejected regardless of policy."""
        policy = create_allowed_policy(allowed + ["*"])
        assert not is_command_allowed(policy, cmd)

    def test_allow_all_wildcard(self) -> None:
        policy = create_allowed_policy(["/bin/ls", "*", "cat"])
        assert "*" in policy.entries
        assert is_command_allowed(policy, "/usr/bin/python -c 'import os'")
        assert is_command_allowed(policy, "anything")

    def test_empty_policy_denies_all(self) -> None:
        policy = create_allowed_policy([])
        assert not is_command_allowed(policy, "ls")


class TestTopicAuthorization:
    @given(entry=st.sampled_from(list(protocol.TOPIC_AUTH_MAP.items())))
    def test_default_policy_allows_all_tracked_actions(self, entry: tuple[tuple[str, str], str]) -> None:
        """Property: Default permissive policy allows all tracked service actions."""
        (topic, action), _ = entry
        assert allows_topic(_make_auth(), topic, action)

    @given(entry=st.sampled_from(list(protocol.TOPIC_AUTH_MAP.items())))
    def test_selective_action_denial_and_case_insensitivity(self, entry: tuple[tuple[str, str], str]) -> None:
        """Property: Disabling a specific permission flag rejects the action under any case variation."""
        (topic, action), field_name = entry
        auth = _make_auth(**{field_name: False})
        assert not allows_topic(auth, topic.upper(), action.upper())
        assert not allows_topic(auth, topic.lower(), action.lower())

    @given(unknown_topic=st.text(alphabet="xyz123", min_size=6, max_size=10), action=_SAFE_TOKEN)
    def test_unknown_topics_and_actions_denied(self, unknown_topic: str, action: str) -> None:
        """Property: Any topic or action not defined in protocol authorization rules defaults to deny."""
        auth = _make_auth()
        assert not allows_topic(auth, unknown_topic, action)
