"""Security policies for YunBridge components."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from .common import normalise_allowed_commands
from .const import ALLOWED_COMMAND_WILDCARD


@dataclass(frozen=True, slots=True)
class AllowedCommandPolicy:
    """Normalised allow-list for shell/process commands."""

    entries: Tuple[str, ...]

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

    def as_tuple(self) -> Tuple[str, ...]:
        return self.entries

    @classmethod
    def from_iterable(
        cls,
        entries: Iterable[str],
    ) -> "AllowedCommandPolicy":
        normalised = normalise_allowed_commands(entries)
        return cls(entries=normalised)


__all__ = ["AllowedCommandPolicy"]
