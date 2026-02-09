"""Security policies for McuBridge components."""

from __future__ import annotations

import shlex
import msgspec
import fnmatch
from collections.abc import Iterable
from typing import Final

from .config.common import normalise_allowed_commands
from .config.const import ALLOWED_COMMAND_WILDCARD
from .protocol.topics import Topic
from .protocol.protocol import (
    AnalogAction,
    ConsoleAction,
    DatastoreAction,
    DigitalAction,
    FileAction,
    MailboxAction,
    ShellAction,
)


class CommandValidationError(Exception):
    """Raised when an inbound command string is unsafe or malformed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def tokenize_shell_command(command: str) -> tuple[str, ...]:
    """Split and validate a shell command string with McuBridge policy.

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


class AllowedCommandPolicy(msgspec.Struct, frozen=True):
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

        cmd = pieces[0].lower()
        for pattern in self.entries:
            if fnmatch.fnmatch(cmd, pattern):
                return True
        return False

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


class TopicAuthorization(msgspec.Struct, frozen=True):
    """Per-topic allow flags for MQTT-driven actions.

    Optimized for lookup speed using a pre-calculated frozenset of allowed (topic, action) tuples.
    """

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

    # Cache for allowed permissions (not serialized)
    _allowed_cache: Final[frozenset[tuple[str, str]]] = frozenset()

    def __post_init__(self) -> None:
        """Build the optimized lookup cache."""
        allowed: list[tuple[str, str]] = []
        # Fast iteration over static mapping
        for key, attr in _TOPIC_AUTH_MAPPING.items():
            if getattr(self, attr):
                allowed.append(key)

        object.__setattr__(self, "_allowed_cache", frozenset(allowed))

    def allows(self, topic: str, action: str) -> bool:
        """Check if action is allowed on topic. O(1) complexity."""
        return (topic.lower(), action.lower()) in self._allowed_cache


# Static mapping to avoid recreation in __post_init__
_TOPIC_AUTH_MAPPING: Final[dict[tuple[str, str], str]] = {
    (Topic.FILE.value, FileAction.READ.value): "file_read",
    (Topic.FILE.value, FileAction.WRITE.value): "file_write",
    (Topic.FILE.value, FileAction.REMOVE.value): "file_remove",
    (Topic.DATASTORE.value, DatastoreAction.GET.value): "datastore_get",
    (Topic.DATASTORE.value, DatastoreAction.PUT.value): "datastore_put",
    (Topic.MAILBOX.value, MailboxAction.READ.value): "mailbox_read",
    (Topic.MAILBOX.value, MailboxAction.WRITE.value): "mailbox_write",
    (Topic.SHELL.value, ShellAction.RUN.value): "shell_run",
    (Topic.SHELL.value, ShellAction.RUN_ASYNC.value): "shell_run_async",
    (Topic.SHELL.value, ShellAction.POLL.value): "shell_poll",
    (Topic.SHELL.value, ShellAction.KILL.value): "shell_kill",
    (Topic.CONSOLE.value, ConsoleAction.IN.value): "console_input",
    (Topic.CONSOLE.value, ConsoleAction.INPUT.value): "console_input",
    (Topic.DIGITAL.value, DigitalAction.WRITE.value): "digital_write",
    (Topic.DIGITAL.value, DigitalAction.READ.value): "digital_read",
    (Topic.DIGITAL.value, DigitalAction.MODE.value): "digital_mode",
    (Topic.ANALOG.value, AnalogAction.WRITE.value): "analog_write",
    (Topic.ANALOG.value, AnalogAction.READ.value): "analog_read",
}

__all__ = [
    "AllowedCommandPolicy",
    "TopicAuthorization",
    "CommandValidationError",
    "tokenize_shell_command",
]
