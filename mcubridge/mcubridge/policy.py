"""Security policies for McuBridge components."""

from __future__ import annotations

import shlex
from typing import Final

from .protocol.structures import AllowedCommandPolicy, TopicAuthorization


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
    except ValueError as exc:
        raise CommandValidationError("Malformed command syntax") from exc
    if not tokens:
        raise CommandValidationError("Empty command")
    for token in tokens:
        if not token:
            raise CommandValidationError("Malformed command syntax")
        # REMOVED: Forbidden char checks. Safe due to execve usage.
    return tokens


__all__ = [
    "AllowedCommandPolicy",
    "TopicAuthorization",
    "CommandValidationError",
    "tokenize_shell_command",
]
