"""Payload validation models for MCU Bridge services."""

from __future__ import annotations

from typing import Annotated

import msgspec
from mcubridge.protocol.protocol import UINT16_MAX

# Constraints for msgspec validation
MAX_COMMAND_LEN = 512


class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ShellCommandPayload(msgspec.Struct, frozen=True):
    """Represents a shell command request coming from MQTT.

    Accepts either plain text or MsgPack: {"command": "..."}.
    """

    command: Annotated[str, msgspec.Meta(min_length=1, max_length=MAX_COMMAND_LEN)]

    @classmethod
    def from_mqtt(cls, payload: bytes) -> ShellCommandPayload:
        """Parse MQTT payload into a validated ShellCommandPayload."""
        if not payload:
            raise PayloadValidationError("Shell command payload is empty")

        # Try msgpack format first
        try:
            result = msgspec.msgpack.decode(payload, type=cls)
            normalized = result.command.strip()
            if not normalized:
                raise PayloadValidationError("Shell command payload is empty")
            return cls(command=normalized)
        except (msgspec.ValidationError, msgspec.DecodeError):
            pass

        # Fallback to plain text command
        text = payload.decode("utf-8", errors="ignore").strip()
        if not text:
            raise PayloadValidationError("Shell command payload is empty")

        if len(text) > MAX_COMMAND_LEN:
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
            return msgspec.convert({"pid": value}, cls, strict=True)
        except (ValueError, msgspec.ValidationError) as exc:
            raise PayloadValidationError(f"Invalid PID segment: {exc}") from exc
