"""Typed payload models for MQTT-driven actions."""
from __future__ import annotations

import json
from typing import Any, Iterable

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    ValidationError,
    Field,
)

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


def _format_validation_errors(errors: Iterable[dict[str, Any]]) -> str:
    messages: list[str] = []
    for error in errors:
        loc = ".".join(
            str(part) if not isinstance(part, slice) else "slice"
            for part in error.get("loc", ())
        )
        prefix = f"{loc}: " if loc else ""
        messages.append(f"{prefix}{error.get('msg', 'Invalid payload')}")
    return "; ".join(messages)


class ShellCommandPayload(BaseModel):
    """Represents a shell command request coming from MQTT."""

    model_config = ConfigDict(str_strip_whitespace=True)

    command: str = Field(min_length=1, max_length=512)

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

        try:
            return cls.model_validate(candidate)
        except ValidationError as exc:  # pragma: no cover - forwarded to caller
            details = [dict(error) for error in exc.errors()]
            raise PayloadValidationError(_format_validation_errors(details))


class ShellPidPayload(BaseModel):
    """MQTT payload specifying an async shell PID to operate on."""

    pid: PositiveInt = Field(le=0xFFFF)

    @classmethod
    def from_topic_segment(cls, segment: str) -> "ShellPidPayload":
        try:
            candidate = {"pid": int(segment, 10)}
        except ValueError as exc:
            raise PayloadValidationError("PID segment must be an integer") from exc
        try:
            return cls.model_validate(candidate)
        except ValidationError as exc:
            details = [dict(error) for error in exc.errors()]
            raise PayloadValidationError(_format_validation_errors(details))
