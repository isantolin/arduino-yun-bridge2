"""Typed payload models for MQTT-driven actions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, cast

__all__ = [
    "PayloadValidationError",
    "ShellCommandPayload",
    "ShellPidPayload",
]


class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(slots=True)
class ShellCommandPayload:
    """Represents a shell command request coming from MQTT."""

    command: str

    @classmethod
    def from_mqtt(cls, payload: bytes) -> "ShellCommandPayload":
        text = payload.decode("utf-8", errors="ignore").strip()
        if not text:
            raise PayloadValidationError("Shell command payload is empty")

        candidate: Any
        if text.startswith("{"):
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise PayloadValidationError("Invalid JSON body") from exc
        else:
            candidate = {"command": text}

        if not isinstance(candidate, dict):
            raise PayloadValidationError("Payload must be an object")

        mapping: Dict[str, Any] = cast(Dict[str, Any], candidate)

        raw_command = mapping.get("command")
        if not isinstance(raw_command, str):
            raise PayloadValidationError("Field 'command' must be a string")

        normalized = raw_command.strip()
        if not normalized:
            raise PayloadValidationError("Shell command payload is empty")
        if len(normalized) > 512:
            raise PayloadValidationError(
                "Command cannot exceed 512 characters"
            )

        return cls(command=normalized)


@dataclass(slots=True)
class ShellPidPayload:
    """MQTT payload specifying an async shell PID to operate on."""

    pid: int

    @classmethod
    def from_topic_segment(cls, segment: str) -> "ShellPidPayload":
        try:
            value = int(segment, 10)
        except ValueError as exc:
            raise PayloadValidationError(
                "PID segment must be an integer"
            ) from exc

        if value <= 0:
            raise PayloadValidationError("PID must be a positive integer")
        if value > 0xFFFF:
            raise PayloadValidationError("PID cannot exceed 65535")
        return cls(pid=value)
