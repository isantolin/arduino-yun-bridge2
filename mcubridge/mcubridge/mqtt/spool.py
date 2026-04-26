"""Durable MQTT publish spool backed by diskcache."""

from __future__ import annotations

import structlog

import msgspec

from ..protocol.structures import QueuedPublish
from ..state.queues import BridgeQueue

logger = structlog.get_logger("mcubridge.mqtt.spool")


class MQTTSpoolError(RuntimeError):
    """Raised when the filesystem spool cannot fulfill an operation."""

    def __init__(
        self,
        reason: str,
        *,
        original: BaseException | None = None,
    ) -> None:
        message = reason if original is None else f"{reason}:{original}"
        super().__init__(message)
        self.reason = reason
        self.original = original


class MQTTPublishSpool:
    """MQTT spool with durable FIFO persistence under /tmp."""

    def __init__(
        self,
        directory: str,
        limit: int,
    ) -> None:
        self._records = BridgeQueue[bytes](
            directory=directory,
            max_items=limit,
        )
        self._corrupt_dropped = 0
        self._dropped_due_to_limit = 0

    def close(self) -> None:
        self._records.close()

    def append(self, message: QueuedPublish) -> None:
        # Use msgspec.json.encode for high-performance direct serialization
        evt = self._records.append(msgspec.json.encode(message))
        if not evt.success:
            raise MQTTSpoolError("append_failed")
        self._dropped_due_to_limit += evt.dropped_chunks

    def pop_next(self) -> QueuedPublish | None:
        while len(self._records) > 0:
            record_bytes = self._records.popleft()
            if record_bytes is None:
                break
            try:
                # Direct JSON decoding into msgspec.Struct
                return msgspec.json.decode(record_bytes, type=QueuedPublish)
            except msgspec.MsgspecError as exc:
                self._corrupt_dropped += 1
                logger.warning("Dropping corrupt MQTT spool entry: %s", exc)
        return None

    def requeue(self, message: QueuedPublish) -> None:
        self._records.appendleft(msgspec.json.encode(message))

    @property
    def pending(self) -> int:
        return len(self._records)

    @property
    def is_degraded(self) -> bool:
        """Return True if the spool is operating in RAM-only mode."""
        return self._records.fallback_active

    @property
    def last_error(self) -> str | None:
        """Return the last error message from the underlying queue."""
        return self._records.last_error

    @property
    def limit(self) -> int:
        return self._records.max_items or 0

    @limit.setter
    def limit(self, value: int) -> None:
        self._records.max_items = max(1, value)

    def snapshot(self) -> dict[str, int | float]:
        return {
            "pending": self.pending,
            "limit": self.limit,
            "dropped_due_to_limit": self._dropped_due_to_limit,
            "corrupt_dropped": self._corrupt_dropped,
            "fallback_active": int(self.is_degraded),
        }


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
