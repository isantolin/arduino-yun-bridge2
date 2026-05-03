"""Durable MQTT publish spool backed by diskcache."""

from __future__ import annotations

import sqlite3
from collections import deque
from typing import Any, cast, TYPE_CHECKING

import diskcache
import msgspec
import structlog

if TYPE_CHECKING:
    pass

from ..protocol.structures import QueuedPublish

logger = structlog.get_logger("mcubridge.mqtt.spool")


class MQTTPublishSpool:
    """MQTT spool with durable FIFO persistence under /tmp."""

    def __init__(
        self,
        directory: str,
        limit: int,
        *,
        on_fallback: Any | None = None,
    ) -> None:
        self._on_fallback = on_fallback
        self.limit = limit
        self._corrupt_dropped = 0
        self._dropped_due_to_limit = 0
        self._directory = directory

        self._cache: diskcache.Cache | None = None
        self._deque: diskcache.Deque | deque[bytes]

        try:
            self._cache = diskcache.Cache(directory)
            self._deque = cast(Any, diskcache.Deque).fromcache(self._cache)
            self._is_degraded = False
            self._last_error = None
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            # [SIL-2] Resilient fallback to RAM if SQLite fails
            if self._cache is not None:
                cast(Any, self._cache).close()
            logger.warning("MQTT spool falling back to RAM: %s", exc)
            self._is_degraded = True
            self._last_error = str(exc)
            self._cache = None
            self._deque = deque(maxlen=limit)

    def close(self) -> None:
        # NOTE: Use "is not None" — diskcache.Cache.__bool__ returns False for
        # empty caches, so "if cache:" would skip close() on empty spools.
        if self._cache is not None:
            cast(Any, self._cache).close()
            self._cache = None
            self._deque = deque(maxlen=self.limit)

    def __del__(self) -> None:
        """Safety net: close the sqlite3 connection if close() was never called.

        [SIL-2] __del__ MUST be completely exception-proof. During interpreter
        shutdown, module globals may be None, so we use self.__dict__ directly
        and catch BaseException to prevent PytestUnraisableExceptionWarning
        (treated as fatal in our test suite with filterwarnings=["error"]).
        """
        try:
            cache = self.__dict__.get("_cache")
            if cache is not None:
                self.__dict__["_cache"] = None
                self.__dict__["_deque"] = deque()
                cache.close()
        except BaseException:  # pylint: disable=broad-except
            pass

    def append(self, message: QueuedPublish) -> None:
        # [SIL-2] Use msgspec.json.encode for high-performance direct serialization
        data = msgspec.json.encode(message)

        if self.limit > 0 and len(self._deque) >= self.limit:
            cast(Any, self._deque).popleft()
            self._dropped_due_to_limit += 1

        cast(Any, self._deque).append(data)

    def pop_next(self) -> QueuedPublish | None:
        while len(self._deque) > 0:
            try:
                record_bytes = cast(Any, self._deque).popleft()
            except (IndexError, AttributeError):
                break

            if record_bytes is None:
                break
            try:
                # Direct JSON decoding into msgspec.Struct
                return msgspec.json.decode(
                    cast(bytes, record_bytes), type=QueuedPublish
                )
            except msgspec.MsgspecError as exc:
                self._corrupt_dropped += 1
                logger.warning("Dropping corrupt MQTT spool entry: %s", exc)
        return None

    def requeue(self, message: QueuedPublish) -> None:
        data = msgspec.json.encode(message)
        if self.limit > 0 and len(self._deque) >= self.limit:
            cast(Any, self._deque).pop()
            self._dropped_due_to_limit += 1
        cast(Any, self._deque).appendleft(data)

    @property
    def pending(self) -> int:
        return len(self._deque)

    @property
    def is_degraded(self) -> bool:
        """Return True if the spool is operating in RAM-only mode."""
        return self._is_degraded

    @property
    def last_error(self) -> str | None:
        """Return the last error message from the underlying queue."""
        return self._last_error

    def snapshot(self) -> dict[str, int | float]:
        return {
            "pending": self.pending,
            "limit": self.limit,
            "dropped_due_to_limit": self._dropped_due_to_limit,
            "corrupt_dropped": self._corrupt_dropped,
            "fallback_active": int(self.is_degraded),
        }


__all__ = ["QueuedPublish", "MQTTPublishSpool"]
