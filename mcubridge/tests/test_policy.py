"""Tests for security policy objects."""

from __future__ import annotations

import pytest
from mcubridge.protocol.structures import (
    create_allowed_policy,
    is_command_allowed,
    allows_topic,
)
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import Topic


def make_topic_auth(**kwargs: bool) -> pb.TopicAuthorization:
    policy = pb.TopicAuthorization()
    for field in [f.name for f in policy.DESCRIPTOR.fields]:
        setattr(policy, field, kwargs.get(field, True))
    return policy


class TestAllowedCommandPolicy:
    def test_allow_all_wildcard(self) -> None:
        """Verify the wildcard allows any command."""
        policy = create_allowed_policy(["/bin/ls", "*", "cat"])
        assert "*" in policy.entries
        assert is_command_allowed(policy, "/usr/bin/python -c 'import os'")
        assert is_command_allowed(policy, "anything")
        assert not is_command_allowed(policy, "  ")  # Empty/whitespace is not allowed

    def test_specific_commands_are_normalized(self) -> None:
        """Verify commands are lowercased and matched correctly."""
        policy = create_allowed_policy(["/bin/ls", "CAT", "dmesg "])
        assert "*" not in policy.entries
        assert is_command_allowed(policy, "/bin/ls -la")
        assert is_command_allowed(policy, "cat /etc/passwd")
        assert is_command_allowed(policy, "dmesg")
        assert not is_command_allowed(policy, "/bin/grep")
        assert not is_command_allowed(policy, "  ")

    def test_only_first_token_is_checked(self) -> None:
        """Verify only the command itself is checked, not arguments."""
        policy = create_allowed_policy(["ls"])
        assert is_command_allowed(policy, "ls -la /")
        assert not is_command_allowed(policy, "/bin/ls")

    def test_empty_policy_allows_nothing(self) -> None:
        """Verify an empty policy denies all commands."""
        policy = create_allowed_policy([])
        assert "*" not in policy.entries
        assert not is_command_allowed(policy, "ls")
        assert not is_command_allowed(policy, "")


class TestTopicAuthorization:
    @pytest.mark.parametrize(
        "topic, action",
        [
            (Topic.FILE.value, "read"),
            (Topic.FILE.value, "write"),
            (Topic.FILE.value, "remove"),
            (Topic.DATASTORE.value, "get"),
            (Topic.DATASTORE.value, "put"),
            (Topic.MAILBOX.value, "read"),
            (Topic.MAILBOX.value, "write"),
            (Topic.SHELL.value, "run_async"),
            (Topic.SHELL.value, "poll"),
            (Topic.SHELL.value, "kill"),
            (Topic.CONSOLE.value, "in"),
            (Topic.DIGITAL.value, "write"),
            (Topic.DIGITAL.value, "read"),
            (Topic.DIGITAL.value, "mode"),
            (Topic.ANALOG.value, "write"),
            (Topic.ANALOG.value, "read"),
        ],
    )
    def test_default_policy_allows_all_tracked_actions(self, topic: str, action: str) -> None:
        """Verify a default policy allows all tracked actions."""
        policy = make_topic_auth()
        assert allows_topic(policy, topic, action) is True

    @pytest.mark.parametrize(
        "topic, action",
        [
            ("unknown_topic", "read"),
            (Topic.FILE.value, "unknown_action"),
            (Topic.CONSOLE.value, ""),
        ],
    )
    def test_default_policy_denies_unknown_actions(self, topic: str, action: str) -> None:
        """Verify topic/action pairs outside the map default to deny."""
        policy = make_topic_auth()
        assert allows_topic(policy, topic, action) is False

    def test_selective_denial(self) -> None:
        """Verify that specific actions can be denied."""
        policy = make_topic_auth(
            file_write=False,
            datastore_put=False,
            shell_run_async=False,
        )
        assert allows_topic(policy, Topic.FILE.value, "write") is False
        assert allows_topic(policy, Topic.DATASTORE.value, "put") is False
        assert allows_topic(policy, Topic.SHELL.value, "run_async") is False

        # Check that others are still allowed
        assert allows_topic(policy, Topic.FILE.value, "read") is True
        assert allows_topic(policy, Topic.DATASTORE.value, "get") is True
        assert allows_topic(policy, Topic.MAILBOX.value, "write") is True
        assert allows_topic(policy, Topic.SHELL.value, "kill") is True

    @pytest.mark.parametrize(
        "kwargs, topic, action",
        [
            ({"console_input": False}, Topic.CONSOLE.value, "input"),
            ({"digital_write": False}, Topic.DIGITAL.value, "write"),
            ({"digital_read": False}, Topic.DIGITAL.value, "read"),
            ({"digital_mode": False}, Topic.DIGITAL.value, "mode"),
            ({"analog_write": False}, Topic.ANALOG.value, "write"),
            ({"analog_read": False}, Topic.ANALOG.value, "read"),
        ],
    )
    def test_console_and_pin_toggles_respected(self, kwargs: dict[str, bool], topic: str, action: str) -> None:
        policy = make_topic_auth(**kwargs)
        assert allows_topic(policy, topic, action) is False

    def test_case_insensitivity(self) -> None:
        """Verify topic and action matching is case-insensitive."""
        policy = make_topic_auth(file_read=False)
        assert allows_topic(policy, "FiLe", "ReAd") is False
        assert allows_topic(policy, "file", "read") is False
