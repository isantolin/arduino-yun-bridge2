"""Security policies for McuBridge components."""

from __future__ import annotations

import msgspec

from .protocol.topics import Topic
from .rpc.protocol import (
    AnalogAction,
    ConsoleAction,
    DatastoreAction,
    DigitalAction,
    FileAction,
    MailboxAction,
)


class TopicAuthorization(msgspec.Struct, frozen=True):
    """Per-topic allow flags for MQTT-driven actions."""

    file_read: bool = True
    file_write: bool = True
    file_remove: bool = True
    datastore_get: bool = True
    datastore_put: bool = True
    mailbox_read: bool = True
    mailbox_write: bool = True
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
            (Topic.FILE.value, FileAction.READ.value): self.file_read,
            (Topic.FILE.value, FileAction.WRITE.value): self.file_write,
            (Topic.FILE.value, FileAction.REMOVE.value): self.file_remove,
            (Topic.DATASTORE.value, DatastoreAction.GET.value): self.datastore_get,
            (Topic.DATASTORE.value, DatastoreAction.PUT.value): self.datastore_put,
            (Topic.MAILBOX.value, MailboxAction.READ.value): self.mailbox_read,
            (Topic.MAILBOX.value, MailboxAction.WRITE.value): self.mailbox_write,
            # Console action historically used "input" internally, while MQTT uses "in".
            # Treat both as equivalent to avoid breaking existing UCI configs / callers.
            (Topic.CONSOLE.value, ConsoleAction.IN.value): self.console_input,
            (Topic.CONSOLE.value, ConsoleAction.INPUT.value): self.console_input,
            (Topic.DIGITAL.value, DigitalAction.WRITE.value): self.digital_write,
            (Topic.DIGITAL.value, DigitalAction.READ.value): self.digital_read,
            (Topic.DIGITAL.value, DigitalAction.MODE.value): self.digital_mode,
            (Topic.ANALOG.value, AnalogAction.WRITE.value): self.analog_write,
            (Topic.ANALOG.value, AnalogAction.READ.value): self.analog_read,
        }
        return mapping.get((topic_key, action_key), False)


__all__ = [
    "TopicAuthorization",
]
