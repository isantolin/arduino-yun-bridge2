"""Durable spool for MQTT publish messages backed by :mod:`sqlite3`."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from . import PublishableMessage

logger = logging.getLogger("yunbridge.mqtt.spool")


class MQTTPublishSpool:
    """SQLite-backed spool to avoid losing MQTT publications."""

    def __init__(self, directory: str, limit: int) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = max(0, limit)
        self._db_path = self.directory / "spool.sqlite3"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS spool ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "payload TEXT NOT NULL"
            ")"
        )
        self._pending = self._count_rows()
        if self.limit:
            with self._lock:
                self._trim_locked()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except Exception:
            logger.debug("Failed to close MQTT spool cleanly", exc_info=True)

    def append(self, message: PublishableMessage) -> None:
        payload = json.dumps(message.to_spool_record(), separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO spool (payload) VALUES (?)",
                (payload,),
            )
            self._pending += 1
            if self.limit:
                self._trim_locked()

    def pop_next(self) -> Optional[PublishableMessage]:
        with self._lock:
            while True:
                row = self._conn.execute(
                    "SELECT id, payload FROM spool ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                message_id, payload = row
                self._conn.execute(
                    "DELETE FROM spool WHERE id = ?",
                    (message_id,),
                )
                self._pending = max(0, self._pending - 1)
                try:
                    record = json.loads(payload)
                    return PublishableMessage.from_spool_record(record)
                except Exception:
                    logger.warning(
                        "Dropping corrupt MQTT spool row id=%d", message_id,
                        exc_info=True,
                    )
                    continue

    def requeue(self, message: PublishableMessage) -> None:
        self.append(message)

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def snapshot(self) -> dict[str, int]:
        return {"pending": self.pending, "limit": self.limit}

    def _count_rows(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]
        )

    def _trim_locked(self) -> None:
        if self.limit <= 0 or self._pending <= self.limit:
            return
        surplus = self._pending - self.limit
        self._conn.execute(
            "DELETE FROM spool WHERE id IN ("
            "SELECT id FROM spool ORDER BY id LIMIT ?"
            ")",
            (surplus,),
        )
        self._pending -= surplus


__all__ = ["MQTTPublishSpool"]
