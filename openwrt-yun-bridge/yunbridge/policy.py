"""Security policies for YunBridge components."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from .common import normalise_allowed_commands
from .const import ALLOWED_COMMAND_WILDCARD
from .protocol.topics import Topic


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
        }
        return mapping.get((topic_key, action_key), True)


__all__ = ["AllowedCommandPolicy", "TopicAuthorization"]
