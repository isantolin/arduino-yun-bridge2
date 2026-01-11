"""Tests for security policy objects."""

from __future__ import annotations

import pytest

from mcubridge.policy import AllowedCommandPolicy, TopicAuthorization
from mcubridge.protocol.topics import Topic


class TestAllowedCommandPolicy:
    def test_allow_all_wildcard(self) -> None:
        """Verify the wildcard allows any command."""
        policy = AllowedCommandPolicy.from_iterable(["/bin/ls", "*", "cat"])
        assert policy.allow_all
        assert policy.is_allowed("/usr/bin/python -c 'import os'")
        assert policy.is_allowed("anything")
        assert not policy.is_allowed("  ")  # Empty/whitespace is not allowed

    def test_specific_commands_are_normalized(self) -> None:
        """Verify commands are lowercased and matched correctly."""
        policy = AllowedCommandPolicy.from_iterable(["/bin/ls", "CAT", "dmesg "])
        assert not policy.allow_all
        assert policy.is_allowed("/bin/ls -la")
        assert policy.is_allowed("cat /etc/passwd")
        assert policy.is_allowed("dmesg")
        assert not policy.is_allowed("/bin/grep")
        assert not policy.is_allowed("  ")

    def test_only_first_token_is_checked(self) -> None:
        """Verify only the command itself is checked, not arguments."""
        policy = AllowedCommandPolicy.from_iterable(["ls"])
        assert policy.is_allowed("ls -la /")
        assert not policy.is_allowed("/bin/ls")

    def test_empty_policy_allows_nothing(self) -> None:
        """Verify an empty policy denies all commands."""
        policy = AllowedCommandPolicy.from_iterable([])
        assert not policy.allow_all
        assert not policy.is_allowed("ls")
        assert not policy.is_allowed("")


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
            (Topic.SHELL.value, "run"),
            (Topic.SHELL.value, "run_async"),
            (Topic.SHELL.value, "poll"),
            (Topic.SHELL.value, "kill"),
            (Topic.CONSOLE.value, "input"),
            (Topic.DIGITAL.value, "write"),
            (Topic.DIGITAL.value, "read"),
            (Topic.DIGITAL.value, "mode"),
            (Topic.ANALOG.value, "write"),
            (Topic.ANALOG.value, "read"),
        ],
    )
    def test_default_policy_allows_all_tracked_actions(
        self, topic: str, action: str
    ) -> None:
        """Verify a default policy allows all tracked actions."""
        policy = TopicAuthorization()
        assert policy.allows(topic, action) is True

    @pytest.mark.parametrize(
        "topic, action",
        [
            ("unknown_topic", "read"),
            (Topic.FILE.value, "unknown_action"),
            (Topic.CONSOLE.value, ""),
        ],
    )
    def test_default_policy_denies_unknown_actions(
        self, topic: str, action: str
    ) -> None:
        """Verify topic/action pairs outside the map default to deny."""
        policy = TopicAuthorization()
        assert policy.allows(topic, action) is False

    def test_selective_denial(self) -> None:
        """Verify that specific actions can be denied."""
        policy = TopicAuthorization(
            file_write=False,
            datastore_put=False,
            shell_run=False,
        )
        assert policy.allows(Topic.FILE.value, "write") is False
        assert policy.allows(Topic.DATASTORE.value, "put") is False
        assert policy.allows(Topic.SHELL.value, "run") is False

        # Check that others are still allowed
        assert policy.allows(Topic.FILE.value, "read") is True
        assert policy.allows(Topic.DATASTORE.value, "get") is True
        assert policy.allows(Topic.MAILBOX.value, "write") is True
        assert policy.allows(Topic.SHELL.value, "kill") is True

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
    def test_console_and_pin_toggles_respected(
        self, kwargs: dict[str, bool], topic: str, action: str
    ) -> None:
        policy = TopicAuthorization(**kwargs)
        assert policy.allows(topic, action) is False

    def test_case_insensitivity(self) -> None:
        """Verify topic and action matching is case-insensitive."""
        policy = TopicAuthorization(file_read=False)
        assert policy.allows("FiLe", "ReAd") is False
        assert policy.allows("file", "read") is False
