"""Property-based and unit tests for security policy objects. [SIL-2]"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.structures import create_allowed_policy, is_command_allowed

_SAFE_TOKEN = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12)
_ARGS_STR = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_- ", max_size=10)


def _make_auth(**kwargs: bool) -> pb.TopicAuthorization:
    fields = {f.name: kwargs.get(f.name, True) for f in pb.TopicAuthorization.DESCRIPTOR.fields}
    return pb.TopicAuthorization(**fields)


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


class TestMethodAuthorization:
    @given(spec=st.sampled_from([s for s in protocol.REGISTRY.operations if s.auth_field]))
    def test_default_policy_allows_all_tracked_actions(self, spec: protocol.OperationSpec) -> None:
        """Property: Default permissive policy allows all tracked service operations."""
        assert protocol.REGISTRY.allows_method(_make_auth(), spec)

    @given(spec=st.sampled_from([s for s in protocol.REGISTRY.operations if s.auth_field]))
    def test_selective_action_denial(self, spec: protocol.OperationSpec) -> None:
        """Property: Disabling a specific permission flag rejects the operation."""
        auth = _make_auth(**{spec.auth_field: False})
        assert not protocol.REGISTRY.allows_method(auth, spec)

    def test_unregistered_or_empty_auth_field_denied_without_allow_all(self) -> None:
        """Property: Any operation without an auth_field defaults to deny unless allow_all is set."""
        spec_unprotected = protocol.OperationSpec(command_id=999, name="unprotected")
        auth = _make_auth()
        assert not protocol.REGISTRY.allows_method(auth, spec_unprotected)
        assert protocol.REGISTRY.allows_method(_make_auth(allow_all=True), spec_unprotected)
