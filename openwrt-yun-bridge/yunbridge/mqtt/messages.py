"""MQTT message helpers used by the daemon runtime."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from collections.abc import Iterable as IterableABC
from typing import Any, TypeGuard, cast

SpoolRecord = dict[str, Any]
UserProperty = tuple[str, str]

IterableAny = IterableABC[Any]

def _is_iterable_sequence(value: Any) -> TypeGuard[IterableAny]:
    return isinstance(value, IterableABC) and not isinstance(value, (bytes, str))


def _normalize_user_properties(
    raw: Any,
) -> tuple[UserProperty, ...]:
    if not _is_iterable_sequence(raw):
        return ()

    normalized: list[UserProperty] = []
    for entry in cast(IterableAny, raw):
        if not _is_iterable_sequence(entry):
            continue
        entry_seq = list(cast(IterableAny, entry))
        if len(entry_seq) < 2:
            continue
        normalized.append((str(entry_seq[0]), str(entry_seq[1])))

    return tuple(normalized)


@dataclass(slots=True)
class QueuedPublish:
    """Serializable MQTT publish packet used by the durable spool."""

    topic_name: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: tuple[UserProperty, ...] = ()

    def to_record(self) -> SpoolRecord:
        return {
            "topic_name": self.topic_name,
            "payload": base64.b64encode(self.payload).decode("ascii"),
            "qos": int(self.qos),
            "retain": self.retain,
            "content_type": self.content_type,
            "payload_format_indicator": self.payload_format_indicator,
            "message_expiry_interval": self.message_expiry_interval,
            "response_topic": self.response_topic,
            "correlation_data": (
                base64.b64encode(self.correlation_data).decode("ascii")
                if self.correlation_data is not None
                else None
            ),
            "user_properties": list(self.user_properties),
        }

    @classmethod
    def from_record(cls, record: SpoolRecord) -> "QueuedPublish":
        payload_b64 = str(record.get("payload", ""))
        payload = base64.b64decode(payload_b64.encode("ascii"))

        correlation_raw = record.get("correlation_data")
        correlation_data = None
        if correlation_raw is not None:
            encoded = str(correlation_raw).encode("ascii")
            correlation_data = base64.b64decode(encoded)

        raw_properties = record.get("user_properties")
        user_properties = _normalize_user_properties(raw_properties)

        return cls(
            topic_name=str(record.get("topic_name", "")),
            payload=payload,
            qos=int(record.get("qos", 0)),
            retain=bool(record.get("retain", False)),
            content_type=record.get("content_type"),
            payload_format_indicator=record.get("payload_format_indicator"),
            message_expiry_interval=record.get("message_expiry_interval"),
            response_topic=record.get("response_topic"),
            correlation_data=correlation_data,
            user_properties=user_properties,
        )


__all__ = ["QueuedPublish", "SpoolRecord"]
