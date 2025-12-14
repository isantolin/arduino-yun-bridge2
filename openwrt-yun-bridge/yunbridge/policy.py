"""Security policies for YunBridge components."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from collections.abc import Iterable

from .common import normalise_allowed_commands
from .const import ALLOWED_COMMAND_WILDCARD
from .protocol.topics import Topic


class CommandValidationError(Exception):
    """Raised when an inbound command string is unsafe or malformed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def tokenize_shell_command(command: str) -> tuple[str, ...]:
    """Split and validate a shell command string with YunBridge policy.

    We use shlex to split the command into tokens, respecting quotes.
    We do NOT forbid shell metacharacters like ';' or '&' because we use
    asyncio.create_subprocess_exec (execve) which does not invoke a shell.
    Therefore, these characters are treated as literal arguments, which is safe.
    """

    stripped = command.strip()
    if not stripped:
        raise CommandValidationError("Empty command")
    try:
        tokens = tuple(shlex.split(stripped, posix=True))
    except ValueError as exc:  # pragma: no cover - shlex edge case
        raise CommandValidationError("Malformed command syntax") from exc
    if not tokens:
        raise CommandValidationError("Empty command")
    for token in tokens:
        if not token:
            raise CommandValidationError("Malformed command syntax")
        # REMOVED: Forbidden char checks. Safe due to execve usage.
    return tokens


@dataclass(frozen=True, slots=True)
class AllowedCommandPolicy:
    """Normalised allow-list for shell/process commands."""

    entries: tuple[str, ...]

    @property
    def allow_all(self) -> bool:
        return ALLOWED_COMMAND_WILDCARD in self.entries

    def is_allowed(self, command: str) -> bool:
        pieces = command.strip().split()
        if not pieces:
            return False
        if self.allow_all:
            return True
        return pieces[0].lower() in self.entries

    def __contains__(self, item: str) -> bool:  # pragma: no cover
        return item.lower() in self.entries

    def as_tuple(self) -> tuple[str, ...]:
        return self.entries

    @classmethod
    def from_iterable(
        cls,
        entries: Iterable[str],
    ) -> AllowedCommandPolicy:
        normalised = normalise_allowed_commands(entries)
        return cls(entries=normalised)


@dataclass(frozen=True, slots=True)
class TopicAuthorization:
    """Per-topic allow flags for MQTT-driven actions."""

    file_read: bool = True
    file_write: bool = True
    file_remove: bool = True
    datastore_get: bool = True
    datastore_put: bool = True
    mailbox_read: bool = True
    mailbox_write: bool = True
    shell_run: bool = True
    shell_run_async: bool = True
    shell_poll: bool = True
    shell_kill: bool = True
    console_input: bool = True
    digital_write: bool = True
    digital_read: bool = True
    digital_mode: bool = True
    analog_write: bool = True
    analog_read: bool = True

    def allows(self, topic: str, action: str) -> bool:
        topic_key = topic.lower()
        action_key = action.lower()
        mapping = {
            (Topic.FILE.value, "read"): self.file_read,
            (Topic.FILE.value, "write"): self.file_write,
            (Topic.FILE.value, "remove"): self.file_remove,
            (Topic.DATASTORE.value, "get"): self.datastore_get,
            (Topic.DATASTORE.value, "put"): self.datastore_put,
            (Topic.MAILBOX.value, "read"): self.mailbox_read,
            (Topic.MAILBOX.value, "write"): self.mailbox_write,
            (Topic.SHELL.value, "run"): self.shell_run,
            (Topic.SHELL.value, "run_async"): self.shell_run_async,
            (Topic.SHELL.value, "poll"): self.shell_poll,
            (Topic.SHELL.value, "kill"): self.shell_kill,
            (Topic.CONSOLE.value, "input"): self.console_input,
            (Topic.DIGITAL.value, "write"): self.digital_write,
            (Topic.DIGITAL.value, "read"): self.digital_read,
            (Topic.DIGITAL.value, "mode"): self.digital_mode,
            (Topic.ANALOG.value, "write"): self.analog_write,
            (Topic.ANALOG.value, "read"): self.analog_read,
        }
        return mapping.get((topic_key, action_key), False)


__all__ = [
    "AllowedCommandPolicy",
    "TopicAuthorization",
    "CommandValidationError",
    "tokenize_shell_command",
]
