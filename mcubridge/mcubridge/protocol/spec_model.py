"""Models for protocol specification and internal state tracking."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import msgspec

# --- State Constants ---
PROCESS_STATE_RUNNING: Final[str] = "running"
PROCESS_STATE_FINISHED: Final[str] = "finished"


class ManagedProcess(msgspec.Struct):
    """Internal tracking for a background shell process."""

    pid: int
    command: str
    state: str = "running"
    exit_code: int | None = None
    stdout_buffer: bytearray = msgspec.field(default_factory=bytearray)
    stderr_buffer: bytearray = msgspec.field(default_factory=bytearray)
    start_time: float = 0.0
    last_activity: float = 0.0


class PendingPinRequest(msgspec.Struct):
    """Tracking for an in-flight pin read request."""

    pin: int
    is_analog: bool
    future: Any  # Use Any to avoid forward ref with asyncio.Future
    timestamp: float = 0.0


# --- Protocol Specification Models (Required by generate.py) ---


class EnumField(msgspec.Struct):
    """A field within an enumeration."""

    name: str
    value: int


class EnumDef(msgspec.Struct):
    """A protocol enumeration (Status, Command, etc.)."""

    name: str
    fields: list[EnumField]


class MessageField(msgspec.Struct):
    """A field within a structured message payload."""

    name: str
    type: str


class MessageDef(msgspec.Struct):
    """A structured message payload definition."""

    name: str
    fields: list[MessageField]


class CommandDef(msgspec.Struct):
    """A protocol command definition."""

    name: str
    directions: list[str]
    payload: str | None = None


class ProtocolSpec(msgspec.Struct):
    """Root model for the protocol specification TOML."""

    enums: list[EnumDef]
    messages: list[MessageDef]
    commands: list[CommandDef]

    @staticmethod
    def load(path: Path | str) -> ProtocolSpec:
        """Load protocol specification from a TOML file (SIL-2)."""
        import tomllib

        with Path(path).open("rb") as f:
            data = tomllib.load(f)
        return msgspec.convert(data, ProtocolSpec)
