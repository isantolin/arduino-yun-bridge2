"""MQTT spool management logic (SIL-2)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import msgspec
import structlog

from ..config.const import SPOOL_BACKOFF_MIN_SECONDS, SPOOL_BACKOFF_MAX_SECONDS
from ..mqtt.spool import MQTTPublishSpool, MQTTSpoolError

if TYPE_CHECKING:
    from ..state.context import RuntimeState
    from ..protocol.structures import QueuedPublish

logger = structlog.get_logger("mcubridge.mqtt.spool")


class MqttSpoolManager:
    """Manages the lifecycle and operations of the persistent MQTT spool (Zero-Wrapper)."""

    def __init__(self, state: RuntimeState) -> None:
        self.state = state

    def initialize(self) -> None:
        if not self.state.mqtt_spool_dir or self.state.mqtt_spool_limit <= 0:
            self.disable("disabled", schedule_retry=False)
            return
        try:
            if self.state.mqtt_spool:
                self.state.mqtt_spool.close()

            self.state.mqtt_spool = MQTTPublishSpool(
                self.state.mqtt_spool_dir,
                self.state.mqtt_spool_limit,
                on_fallback=self._on_fallback,
            )
            self.state.mqtt_spool_degraded = self.state.mqtt_spool.is_degraded
        except (OSError, MQTTSpoolError) as exc:
            self._handle_failure("initialization_failed", exc)

    async def ensure_active(self) -> bool:
        if self.state.mqtt_spool:
            return True

        if not self.state.mqtt_spool_dir or self.state.mqtt_spool_limit <= 0:
            return False

        now = time.monotonic()
        if self.state.mqtt_spool_backoff_until > now:
            return False

        try:
            self.state.mqtt_spool = await asyncio.to_thread(
                MQTTPublishSpool,
                self.state.mqtt_spool_dir,
                self.state.mqtt_spool_limit,
                on_fallback=self._on_fallback,
            )
            self.state.mqtt_spool_degraded = self.state.mqtt_spool.is_degraded
            self.state.mqtt_spool_recoveries += 1
            return True
        except (OSError, MQTTSpoolError) as exc:
            self._handle_failure("reactivation_failed", exc)
            return False

    def disable(self, reason: str, schedule_retry: bool = True) -> None:
        if self.state.mqtt_spool:
            with contextlib.suppress(Exception):
                self.state.mqtt_spool.close()
        self.state.mqtt_spool = None
        self.state.mqtt_spool_degraded = True
        self.state.mqtt_spool_failure_reason = reason
        if schedule_retry:
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        self.state.mqtt_spool_retry_attempts = min(
            self.state.mqtt_spool_retry_attempts + 1, 6
        )
        delay = min(
            SPOOL_BACKOFF_MIN_SECONDS
            * (2 ** (self.state.mqtt_spool_retry_attempts - 1)),
            SPOOL_BACKOFF_MAX_SECONDS,
        )
        self.state.mqtt_spool_backoff_until = time.monotonic() + delay

    def _handle_failure(self, reason: str, exc: Exception) -> None:
        self.state.mqtt_spool_errors += 1
        self.state.metrics.mqtt_spool_errors.inc()
        self.state.mqtt_spool_last_error = str(exc)
        self.disable(reason)

    def _on_fallback(self, reason: str, exc: Exception | None = None) -> None:
        self.state.mqtt_spool_degraded = True
        self.state.mqtt_spool_failure_reason = reason
        if exc:
            self.state.mqtt_spool_last_error = str(exc)
        self.state.mqtt_spool_errors += 1
        self.state.metrics.mqtt_spool_errors.inc()

    async def stash(self, message: QueuedPublish) -> bool:
        if not await self.ensure_active():
            return False
        if not self.state.mqtt_spool:
            return False
        try:
            await asyncio.to_thread(self.state.mqtt_spool.append, message)
            self.state.mqtt_spooled_messages += 1
            self.state.metrics.mqtt_spooled_messages.inc()
            return True
        except (MQTTSpoolError, OSError) as exc:
            self._handle_failure("append_failed", exc)
            return False

    async def flush(self) -> None:
        if not await self.ensure_active() or not self.state.mqtt_spool:
            return

        while self.state.mqtt_publish_queue.qsize() < self.state.mqtt_queue_limit:
            try:
                msg = await asyncio.to_thread(self.state.mqtt_spool.pop_next)
                if not msg:
                    break

                # Tag as replayed
                props = list(msg.user_properties) + [("bridge-spooled", "1")]
                final_msg = msgspec.structs.replace(msg, user_properties=tuple(props))

                try:
                    self.state.mqtt_publish_queue.put_nowait(final_msg)
                    self.state.mqtt_spooled_replayed += 1
                except asyncio.QueueFull:
                    await asyncio.to_thread(self.state.mqtt_spool.requeue, msg)
                    break
            except (MQTTSpoolError, OSError) as exc:
                self._handle_failure("pop_failed", exc)
                break
