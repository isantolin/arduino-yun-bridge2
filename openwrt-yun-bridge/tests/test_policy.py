"""Tests for security policy objects."""
from __future__ import annotations

import pytest

from yunbridge.policy import AllowedCommandPolicy, TopicAuthorization
from yunbridge.protocol.topics import Topic


class TestAllowedCommandPolicy:
    def test_allow_all_wildcard(self) -> None:
        """Verify the wildcard allows any command."""
        policy = AllowedCommandPolicy.from_iterable(
            ["/bin/ls", "*", "cat"]
        )
        assert policy.allow_all
        assert policy.is_allowed("/usr/bin/python -c 'import os'")
        assert policy.is_allowed("anything")
        assert not policy.is_allowed("  ")  # Empty/whitespace is not allowed

    def test_specific_commands_are_normalized(self) -> None:
        """Verify commands are lowercased and matched correctly."""
        policy = AllowedCommandPolicy.from_iterable(["/bin/ls",
                                                     "CAT",
                                                     "dmesg "])
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
        "topic, action, expected",
        [
            (Topic.FILE.value, "read", True),
            (Topic.FILE.value, "write", True),
            (Topic.FILE.value, "remove", True),
            (Topic.DATASTORE.value, "get", True),
            (Topic.DATASTORE.value, "put", True),
            (Topic.MAILBOX.value, "read", True),
            (Topic.MAILBOX.value, "write", True),
            ("unknown_topic", "read", True),  # Defaults to True
        ],
    )
    def test_default_policy_allows_all(
        self, topic: str, action: str, expected: bool
    ) -> None:
        """Verify a default policy allows all tracked actions."""
        policy = TopicAuthorization()
        assert policy.allows(topic, action) is expected

    def test_selective_denial(self) -> None:
        """Verify that specific actions can be denied."""
        policy = TopicAuthorization(file_write=False, datastore_put=False)
        assert policy.allows(Topic.FILE.value, "write") is False
        assert policy.allows(Topic.DATASTORE.value, "put") is False

        # Check that others are still allowed
        assert policy.allows(Topic.FILE.value, "read") is True
        assert policy.allows(Topic.DATASTORE.value, "get") is True
        assert policy.allows(Topic.MAILBOX.value, "write") is True

    def test_case_insensitivity(self) -> None:
        """Verify topic and action matching is case-insensitive."""
        policy = TopicAuthorization(file_read=False)
        assert policy.allows("FiLe", "ReAd") is False
        assert policy.allows("file", "read") is False
