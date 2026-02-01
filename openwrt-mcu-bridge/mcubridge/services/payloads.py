"""Typed payload models for MQTT-driven actions.

Uses msgspec.Struct for zero-copy deserialization with built-in validation.
"""

from __future__ import annotations

from typing import Annotated

import msgspec

from mcubridge.rpc.protocol import UINT16_MAX

__all__ = [
    "PayloadValidationError",
    "ShellCommandPayload",
    "ShellPidPayload",
]

# Constraints for msgspec validation
_MAX_COMMAND_LEN = 512


class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ShellCommandPayload(msgspec.Struct, frozen=True):
    """Represents a shell command request coming from MQTT.

    Accepts either plain text or JSON: {"command": "..."}.
    """

    command: Annotated[str, msgspec.Meta(min_length=1, max_length=_MAX_COMMAND_LEN)]

    @classmethod
    def from_mqtt(cls, payload: bytes) -> ShellCommandPayload:
        """Parse MQTT payload into a validated ShellCommandPayload."""
        text = payload.decode("utf-8", errors="ignore").strip()
        if not text:
            raise PayloadValidationError("Shell command payload is empty")

        # Accept both plain text and JSON format
        if text.startswith("{"):
            try:
                result = msgspec.json.decode(text, type=cls)
                # Normalize whitespace
                normalized = result.command.strip()
                if not normalized:
                    raise PayloadValidationError("Shell command payload is empty")
                return cls(command=normalized)
            except msgspec.ValidationError as exc:
                raise PayloadValidationError(str(exc)) from exc
            except msgspec.DecodeError:
                # Malformed JSON - treat entire text as command
                pass

        # Plain text command
        if len(text) > _MAX_COMMAND_LEN:
            raise PayloadValidationError("Command cannot exceed 512 characters")
        return cls(command=text)


class ShellPidPayload(msgspec.Struct, frozen=True):
    """MQTT payload specifying an async shell PID to operate on."""

    pid: Annotated[int, msgspec.Meta(gt=0, le=UINT16_MAX)]

    @classmethod
    def from_topic_segment(cls, segment: str) -> ShellPidPayload:
        """Parse a topic segment into a validated ShellPidPayload."""
        try:
            value = int(segment, 10)
        except ValueError as exc:
            raise PayloadValidationError("PID segment must be an integer") from exc

        # Validate constraints manually since msgspec.Struct only validates during decode
        if value <= 0:
            raise PayloadValidationError("PID must be a positive integer")
        if value > UINT16_MAX:
            raise PayloadValidationError("PID cannot exceed 65535")

        return cls(pid=value)
