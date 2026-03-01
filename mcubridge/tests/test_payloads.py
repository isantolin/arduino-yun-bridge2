"""Tests for payload validation helpers."""

from __future__ import annotations

import pytest
from mcubridge.services.shell import (
    PayloadValidationError,
    ShellCommandPayload,
    ShellPidPayload,
)


def test_shell_command_payload_plain_text() -> None:
    payload = ShellCommandPayload.from_mqtt(b"  ls -la \n")
    assert payload.command == "ls -la"


def test_shell_command_payload_json_body() -> None:
    payload = ShellCommandPayload.from_mqtt(b'{"command": "echo hi"}')
    assert payload.command == "echo hi"


@pytest.mark.parametrize(
    "raw",
    [b"", b"   ", b"{}"],
)
def test_shell_command_payload_rejects_empty(raw: bytes) -> None:
    with pytest.raises(PayloadValidationError):
        ShellCommandPayload.from_mqtt(raw)


def test_shell_command_payload_rejects_long_command() -> None:
    raw = b"a" * 513
    with pytest.raises(PayloadValidationError, match="512"):
        ShellCommandPayload.from_mqtt(raw)


def test_shell_pid_payload_valid_segment() -> None:
    payload = ShellPidPayload.from_topic_segment("42")
    assert payload.pid == 42


@pytest.mark.parametrize("segment", ["0", "-1", "70000", "abc"])
def test_shell_pid_payload_rejects_invalid(segment: str) -> None:
    with pytest.raises(PayloadValidationError):
        ShellPidPayload.from_topic_segment(segment)


# --- Property-based tests replacement ---


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "echo hello",
        "a" * 512,
        "ñandú",
        "123",
    ],
)
def test_shell_command_payload_accepts_valid_utf8(command: str) -> None:
    """Any non-empty UTF-8 ≤512 chars is accepted as raw payload."""
    if not command.strip():
        return  # Empty commands are invalid

    encoded = command.encode("utf-8")
    payload = ShellCommandPayload.from_mqtt(encoded)
    assert payload.command == command.strip()


@pytest.mark.parametrize("pid", [1, 42, 65535])
def test_shell_pid_payload_accepts_valid_range(pid: int) -> None:
    """Verify PID handling for the full valid 16-bit range."""
    segment = str(pid)
    payload = ShellPidPayload.from_topic_segment(segment)
    assert payload.pid == pid


@pytest.mark.parametrize("pid", [0, -1, 65536, 100000])
def test_shell_pid_payload_rejects_invalid_range(pid: int) -> None:
    """Verify rejection of PIDs outside 1-65535."""
    segment = str(pid)
    with pytest.raises(PayloadValidationError):
        ShellPidPayload.from_topic_segment(segment)
