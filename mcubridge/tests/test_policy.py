"""Property-based and unit tests for security policy objects. [SIL-2]"""

from __future__ import annotations

import shlex

from hypothesis import given
from hypothesis import strategies as st
from mcubridge.protocol import config_schema_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.structures import create_allowed_policy, parse_allowed_command

_SAFE_TOKEN = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12)
_ARGS_STR = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_- ", max_size=10)


def _make_auth(**kwargs: bool) -> pb.TopicAuthorization:
    fields = {f.name: kwargs.get(f.name, True) for f in pb.TopicAuthorization.DESCRIPTOR.fields}
    return pb.TopicAuthorization(**fields)


class TestAllowedCommandPolicy:
    @given(allowed=st.lists(_SAFE_TOKEN, min_size=1, max_size=5), args=_ARGS_STR)
    def test_allowed_commands_matched_by_first_token(self, allowed: list[str], args: str) -> None:
        """Property: an allowed program yields exactly the argv that will be executed."""
        policy = create_allowed_policy(allowed)
        cmd = f"{allowed[0]} {args}".strip()
        assert parse_allowed_command(policy, cmd) == shlex.split(cmd)

    @given(allowed=st.lists(_SAFE_TOKEN, min_size=1, max_size=5), cmd=st.text(alphabet=" \t\r\n", max_size=10))
    def test_empty_or_whitespace_always_denied(self, allowed: list[str], cmd: str) -> None:
        """Property: Empty or pure whitespace commands are always rejected regardless of policy."""
        policy = create_allowed_policy(allowed + ["*"])
        assert parse_allowed_command(policy, cmd) is None

    def test_allow_all_wildcard(self) -> None:
        policy = create_allowed_policy(["/bin/ls", "*", "cat"])
        assert "*" in policy.entries
        assert parse_allowed_command(policy, "/usr/bin/python -c 'import os'") == [
            "/usr/bin/python",
            "-c",
            "import os",
        ]
        assert parse_allowed_command(policy, "anything") == ["anything"]

    def test_empty_policy_denies_all(self) -> None:
        policy = create_allowed_policy([])
        assert parse_allowed_command(policy, "ls") is None

    def test_matching_is_case_sensitive(self) -> None:
        policy = create_allowed_policy(["ls", "MyTool"])
        assert tuple(policy.entries) == ("MyTool", "ls")
        assert parse_allowed_command(policy, "LS") is None
        assert parse_allowed_command(policy, "mytool") is None
        assert parse_allowed_command(policy, "MyTool --x") == ["MyTool", "--x"]

    def test_path_entry_requires_exact_path(self) -> None:
        policy = create_allowed_policy(["/bin/ls"])
        assert parse_allowed_command(policy, "/bin/ls -l") == ["/bin/ls", "-l"]
        assert parse_allowed_command(policy, "/bin/../bin/ls") is None
        assert parse_allowed_command(policy, "ls") is None

    def test_globs_never_cross_path_separators(self) -> None:
        name_glob = create_allowed_policy(["py*"])
        assert parse_allowed_command(name_glob, "python3 -V") == ["python3", "-V"]
        assert parse_allowed_command(name_glob, "/usr/bin/python3") is None
        # A "/" entry is a literal path, so "/usr/bin/*" cannot reach outside /usr/bin.
        path_entry = create_allowed_policy(["/usr/bin/*"])
        assert parse_allowed_command(path_entry, "/usr/bin/../../tmp/x") is None
        assert parse_allowed_command(path_entry, "/usr/bin/ls") is None

    def test_malformed_quoting_is_rejected(self) -> None:
        policy = create_allowed_policy(["*"])
        assert parse_allowed_command(policy, 'echo "unterminated') is None
        assert parse_allowed_command(policy, "echo 'unterminated") is None


class TestMethodAuthorization:
    @given(spec=st.sampled_from([s for s in protocol.OPERATIONS if s.auth_field]))
    def test_default_policy_allows_all_tracked_actions(self, spec: protocol.OperationSpec) -> None:
        """Property: Default permissive policy allows all tracked service operations."""
        assert protocol.allows_method(_make_auth(), spec)

    @given(spec=st.sampled_from([s for s in protocol.OPERATIONS if s.auth_field]))
    def test_selective_action_denial(self, spec: protocol.OperationSpec) -> None:
        """Property: Disabling a specific permission flag rejects the operation."""
        auth_field: str = str(spec.auth_field)
        auth = _make_auth(**{auth_field: False})
        assert not protocol.allows_method(auth, spec)

    def test_unregistered_or_empty_auth_field_denied_without_allow_all(self) -> None:
        """Property: Any operation without an auth_field defaults to deny unless allow_all is set."""
        spec_unprotected = protocol.OperationSpec(name="unprotected")
        auth = _make_auth()
        assert not protocol.allows_method(auth, spec_unprotected)

        class AllowAllAuth:
            allow_all = True

        assert protocol.allows_method(AllowAllAuth(), spec_unprotected)
