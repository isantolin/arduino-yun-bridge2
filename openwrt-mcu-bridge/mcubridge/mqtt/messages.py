"""MQTT message helpers used by the daemon runtime."""

from __future__ import annotations

import msgspec
from typing import Any, Iterable, cast

from mcubridge.protocol.structures import QOSLevel, QueuedPublish, SpoolRecord

UserProperty = tuple[str, str]


class QueuedPublish(msgspec.Struct):
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
        """Convert to a SpoolRecord struct for disk serialization."""
        return SpoolRecord(
            topic_name=self.topic_name,
            payload=self.payload,  # MessagePack handles bytes natively
            qos=int(self.qos),
            retain=self.retain,
            content_type=self.content_type,
            payload_format_indicator=self.payload_format_indicator,
            message_expiry_interval=self.message_expiry_interval,
            response_topic=self.response_topic,
            correlation_data=self.correlation_data,  # MessagePack handles bytes natively
            user_properties=list(self.user_properties),
        )

    @classmethod
    def from_record(cls, record: SpoolRecord | dict[str, Any]) -> QueuedPublish:
        """Create a QueuedPublish instance from a SpoolRecord struct or dict."""
        data: dict[str, Any] = record if isinstance(record, dict) else msgspec.structs.asdict(record)

        payload = data.get("payload", b"")
        if isinstance(payload, str):
             payload = payload.encode("utf-8") # Fallback if legacy JSON somehow persisted

        correlation_data = data.get("correlation_data")
        if isinstance(correlation_data, str):
             correlation_data = correlation_data.encode("utf-8") # Fallback

        raw_props = data.get("user_properties", ())
        user_properties: list[tuple[str, str]] = []  # pyright: ignore[reportUnknownVariableType]
        if isinstance(raw_props, Iterable):
            for item in cast("Iterable[Any]", raw_props):
                if isinstance(item, (list, tuple)) and len(item) >= 2:  # pyright: ignore[reportUnknownArgumentType]
                    k = str(item[0])  # pyright: ignore[reportUnknownArgumentType]
                    v = str(item[1])  # pyright: ignore[reportUnknownArgumentType]
                    user_properties.append((k, v))

        return cls(
            topic_name=str(data.get("topic_name", "")),
            payload=payload,
            qos=int(data.get("qos", 0)),
            retain=bool(data.get("retain", False)),
            content_type=data.get("content_type"),
            payload_format_indicator=data.get("payload_format_indicator"),
            message_expiry_interval=data.get("message_expiry_interval"),
            response_topic=data.get("response_topic"),
            correlation_data=correlation_data,
            user_properties=tuple(user_properties),  # pyright: ignore[reportUnknownArgumentType]
        )


class SpoolRecord(msgspec.Struct, omit_defaults=True):
    """MessagePack-serializable record stored in the durable spool."""

    topic_name: str
    payload: bytes  # Changed from str (base64) to bytes for msgpack
    qos: int = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None # Changed from str (base64) to bytes
    user_properties: list[tuple[str, str]] = msgspec.field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


__all__ = ["QOSLevel", "QueuedPublish", "SpoolRecord"]
