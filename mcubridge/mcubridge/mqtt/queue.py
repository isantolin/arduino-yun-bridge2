"""MQTT queue and spool management (Zero-Wrapper)."""

from __future__ import annotations

import asyncio
import contextlib
import structlog
import time
from typing import Any, cast

import msgspec
from aiomqtt.message import Message

from mcubridge.config.const import SPOOL_BACKOFF_MIN_SECONDS, SPOOL_BACKOFF_MAX_SECONDS
from mcubridge.mqtt.spool import MQTTPublishSpool, MQTTSpoolError
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.mqtt.queue")


def configure_spool(state: RuntimeState, directory: str, limit: int) -> None:
    if state.mqtt_spool:
        cast(Any, state.mqtt_spool).close()
        state.mqtt_spool = None
    state.mqtt_spool_dir = directory
    state.mqtt_spool_limit = max(0, limit)


def _handle_spool_failure(
    state: RuntimeState, reason: str, exc: BaseException | None = None
) -> None:
    state.mqtt_spool_errors += 1
    state.metrics.mqtt_spool_errors.inc()
    if exc:
        state.mqtt_spool_last_error = str(exc)

    if state.mqtt_spool:
        with contextlib.suppress(OSError, AttributeError):
            cast(Any, state.mqtt_spool).close()
    state.mqtt_spool = None
    state.mqtt_spool_degraded = True
    state.mqtt_spool_failure_reason = reason

    state.mqtt_spool_retry_attempts = min(state.mqtt_spool_retry_attempts + 1, 6)
    delay = min(
        SPOOL_BACKOFF_MIN_SECONDS * (2 ** (state.mqtt_spool_retry_attempts - 1)),
        SPOOL_BACKOFF_MAX_SECONDS,
    )
    state.mqtt_spool_backoff_until = time.monotonic() + delay


def _on_spool_fallback(
    state: RuntimeState, reason: str, exc: BaseException | None = None
) -> None:
    state.mqtt_spool_degraded = True
    state.mqtt_spool_failure_reason = reason
    if exc:
        state.mqtt_spool_last_error = str(exc)
    state.mqtt_spool_errors += 1
    state.metrics.mqtt_spool_errors.inc()


def initialize_spool(state: RuntimeState) -> None:
    if not state.mqtt_spool_dir or state.mqtt_spool_limit <= 0:
        _handle_spool_failure(state, "disabled")
        return
    try:
        if state.mqtt_spool:
            cast(Any, state.mqtt_spool).close()
            state.mqtt_spool = None

        def _fallback_wrapper(r: Any, e: Any = None) -> None:
            _on_spool_fallback(state, str(r), e)

        spool_obj = cast(Any, MQTTPublishSpool)(
            state.mqtt_spool_dir,
            state.mqtt_spool_limit,
            on_fallback=_fallback_wrapper,
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
    if state.mqtt_spool:
        return True

    backoff_remaining = (
        max(0.0, state.mqtt_spool_backoff_until - time.monotonic())
        if state.mqtt_spool_backoff_until > 0
        else 0.0
    )

    if not state.mqtt_spool_dir or state.mqtt_spool_limit <= 0 or backoff_remaining > 0:
        return False
    try:

        def _fallback_wrapper(r: Any, e: Any = None) -> None:
            _on_spool_fallback(state, str(r), e)

        state.mqtt_spool = await asyncio.to_thread(
            cast(Any, MQTTPublishSpool),
            state.mqtt_spool_dir,
            state.mqtt_spool_limit,
            on_fallback=_fallback_wrapper,
        )
        if cast(Any, state.mqtt_spool).is_degraded:
            state.mqtt_spool_degraded = True
            state.mqtt_spool_failure_reason = (
                cast(Any, state.mqtt_spool).last_error or "reactivation_failed"
            )
            state.mqtt_spool_last_error = cast(Any, state.mqtt_spool).last_error
        else:
            state.mqtt_spool_degraded = False
            state.mqtt_spool_failure_reason = None
        state.mqtt_spool_recoveries += 1
        return True
    except (OSError, MQTTSpoolError) as exc:
        _handle_spool_failure(state, "reactivation_failed", exc=exc)
        return False


async def stash_mqtt_message(state: RuntimeState, message: QueuedPublish) -> bool:
    if not await ensure_spool(state):
        return False
    spool = state.mqtt_spool
    if spool is None:
        return False
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, cast(Any, spool).append, message)
        state.mqtt_spooled_messages += 1
        state.metrics.mqtt_spooled_messages.inc()
        return True
    except (MQTTSpoolError, OSError) as exc:
        _handle_spool_failure(state, "append_failed", exc=exc)
        return False


async def flush_mqtt_spool(state: RuntimeState) -> None:
    if not await ensure_spool(state):
        return
    spool = state.mqtt_spool
    if spool is None:
        return
    while state.mqtt_publish_queue.qsize() < state.mqtt_queue_limit:
        try:
            msg = await asyncio.to_thread(cast(Any, spool).pop_next)
            if not msg:
                break
            props = list(msg.user_properties) + [("bridge-spooled", "1")]
            final_msg = msgspec.structs.replace(msg, user_properties=props)
            try:
                state.mqtt_publish_queue.put_nowait(final_msg)
                state.mqtt_spooled_replayed += 1
            except asyncio.QueueFull:
                await asyncio.to_thread(cast(Any, spool).requeue, msg)
                break
        except (MQTTSpoolError, OSError) as exc:
            _handle_spool_failure(state, "pop_failed", exc=exc)
            break


async def enqueue_mqtt(
    state: RuntimeState,
    message: QueuedPublish,
    *,
    reply_context: Message | None = None,
) -> None:
    """Enqueues an MQTT message for publishing with an overflow dropping strategy."""
    message_to_queue = message
    if reply_context is not None:
        props = reply_context.properties
        target_topic = (
            getattr(props, "ResponseTopic", None) if props else None
        ) or message.topic_name
        if target_topic != message_to_queue.topic_name:
            message_to_queue = msgspec.structs.replace(
                message_to_queue, topic_name=target_topic
            )

        reply_correlation = getattr(props, "CorrelationData", None) if props else None
        if reply_correlation is not None:
            message_to_queue = msgspec.structs.replace(
                message_to_queue, correlation_data=reply_correlation
            )

        origin_topic = str(reply_context.topic)
        new_props = message_to_queue.user_properties + (
            ("bridge-request-topic", origin_topic),
        )
        message_to_queue = msgspec.structs.replace(
            message_to_queue, user_properties=new_props
        )

    try:
        state.mqtt_publish_queue.put_nowait(message_to_queue)
    except (asyncio.QueueFull, asyncio.queues.QueueFull):
        try:
            dropped = state.mqtt_publish_queue.get_nowait()
            state.mqtt_publish_queue.task_done()
            state.mqtt_drop_counts[dropped.topic_name] = (
                state.mqtt_drop_counts.get(dropped.topic_name, 0) + 1
            )
            state.mqtt_dropped_messages += 1
            state.metrics.mqtt_messages_dropped.inc()

            await stash_mqtt_message(state, dropped)

            state.mqtt_publish_queue.put_nowait(message_to_queue)

            logger.warning(
                "MQTT publish queue saturated; dropped oldest message from topic=%s",
                dropped.topic_name,
            )
        except (asyncio.QueueEmpty, asyncio.queues.QueueEmpty):
            state.mqtt_publish_queue.put_nowait(message_to_queue)
