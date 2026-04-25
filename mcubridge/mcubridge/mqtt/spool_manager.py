"""MQTT Spool management logic for reliable message delivery (SIL-2)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

import structlog

from mcubridge.config.const import SPOOL_BACKOFF_MIN_SECONDS, SPOOL_BACKOFF_MAX_SECONDS
from .spool import MQTTPublishSpool, MQTTSpoolError

if TYPE_CHECKING:
    from mcubridge.state.context import RuntimeState
    from mcubridge.protocol.structures import QueuedPublish

logger = structlog.get_logger("mcubridge.mqtt.spool")


def initialize_spool(state: RuntimeState) -> None:
    """Initialize or reconfigure the MQTT spool based on state settings."""
    if not state.mqtt_spool_dir or state.mqtt_spool_limit <= 0:
        _disable_spool(state, "disabled", schedule_retry=False)
        return
    try:
        if state.mqtt_spool:
            with contextlib.suppress(OSError, AttributeError):
                state.mqtt_spool.close()
            state.mqtt_spool = None

        spool_obj = MQTTPublishSpool(
            state.mqtt_spool_dir,
            state.mqtt_spool_limit,
            on_fallback=lambda r, e: _on_spool_fallback(state, r, e),
        )
        state.mqtt_spool = spool_obj
        if spool_obj.is_degraded:
            state.mqtt_spool_degraded = True
            state.mqtt_spool_failure_reason = (
                spool_obj.last_error or "initialization_failed"
            )
            state.mqtt_spool_last_error = spool_obj.last_error
        else:
            state.mqtt_spool_degraded = False
            state.mqtt_spool_failure_reason = None
    except (OSError, MQTTSpoolError) as exc:
        _handle_spool_failure(state, "initialization_failed", exc=exc)


async def ensure_spool(state: RuntimeState) -> bool:
    """Ensure the MQTT spool is active, attempting recovery if needed."""
    if state.mqtt_spool:
        return True

    backoff = (
        max(0.0, state.mqtt_spool_backoff_until - time.monotonic())
        if state.mqtt_spool_backoff_until > 0
        else 0.0
    )

    if not state.mqtt_spool_dir or state.mqtt_spool_limit <= 0 or backoff > 0:
        return False

    try:
        # Re-initialization is blocking on DB open, run in executor
        loop = asyncio.get_running_loop()
        state.mqtt_spool = await loop.run_in_executor(
            None,
            MQTTPublishSpool,
            state.mqtt_spool_dir,
            state.mqtt_spool_limit,
            lambda r, e: _on_spool_fallback(state, r, e),
        )
        if state.mqtt_spool.is_degraded:
            state.mqtt_spool_degraded = True
            state.mqtt_spool_failure_reason = (
                state.mqtt_spool.last_error or "reactivation_failed"
            )
            state.mqtt_spool_last_error = state.mqtt_spool.last_error
        else:
            state.mqtt_spool_degraded = False
            state.mqtt_spool_failure_reason = None
        state.mqtt_spool_recoveries += 1
        return True
    except (OSError, MQTTSpoolError) as exc:
        _handle_spool_failure(state, "reactivation_failed", exc=exc)
        return False


async def stash_message(state: RuntimeState, message: QueuedPublish) -> bool:
    """Persistently store an MQTT message in the spool."""
    if not await ensure_spool(state):
        return False
    spool: MQTTPublishSpool | None = state.mqtt_spool
    if spool is None:
        return False
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, spool.append, message)
        state.mqtt_spooled_messages += 1
        state.metrics.mqtt_spooled_messages.inc()
        return True
    except (MQTTSpoolError, OSError) as exc:
        _handle_spool_failure(state, "append_failed", exc=exc)
        return False


async def flush_spool(state: RuntimeState, client_publish_func: Any) -> None:
    """Flush spooled messages back to the primary publish mechanism."""
    if not await ensure_spool(state):
        return
    spool: MQTTPublishSpool | None = state.mqtt_spool
    if spool is None:
        return

    try:
        loop = asyncio.get_running_loop()
        while True:
            msg = await loop.run_in_executor(None, spool.pop)
            if msg is None:
                break
            try:
                await client_publish_func(msg)
            except Exception:
                # Re-append on failure and stop flushing
                await loop.run_in_executor(None, spool.append, msg)
                break
    except (MQTTSpoolError, OSError) as exc:
        _handle_spool_failure(state, "flush_failed", exc=exc)


def _disable_spool(
    state: RuntimeState, reason: str, schedule_retry: bool = True
) -> None:
    if state.mqtt_spool:
        with contextlib.suppress(OSError, AttributeError):
            state.mqtt_spool.close()
    state.mqtt_spool = None
    state.mqtt_spool_degraded = True
    state.mqtt_spool_failure_reason = reason
    if schedule_retry:
        state.mqtt_spool_retry_attempts = min(state.mqtt_spool_retry_attempts + 1, 6)
        delay = min(
            SPOOL_BACKOFF_MIN_SECONDS * (2 ** (state.mqtt_spool_retry_attempts - 1)),
            SPOOL_BACKOFF_MAX_SECONDS,
        )
        state.mqtt_spool_backoff_until = time.monotonic() + delay


def _handle_spool_failure(
    state: RuntimeState, reason: str, exc: BaseException | None = None
) -> None:
    state.mqtt_spool_errors += 1
    state.metrics.mqtt_spool_errors.inc()
    if exc:
        state.mqtt_spool_last_error = str(exc)
    _disable_spool(state, reason)


def _on_spool_fallback(
    state: RuntimeState, reason: str, exc: BaseException | None = None
) -> None:
    state.mqtt_spool_degraded = True
    state.mqtt_spool_failure_reason = reason
    if exc:
        state.mqtt_spool_last_error = str(exc)
    state.mqtt_spool_errors += 1
    state.metrics.mqtt_spool_errors.inc()
