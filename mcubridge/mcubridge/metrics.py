"""Periodic metrics publisher for MCU Bridge (Pure Telemetry Push, SIL-2)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog

from .config import const
from .protocol import mcubridge_pb2 as pb
from .protocol.structures import PROTOBUF_CONTENT_TYPE, create_queued_publish
from .protocol.topics import Topic, topic_path
from .state.context import RuntimeState

logger = structlog.get_logger("mcubridge.metrics")

PublishEnqueue = Callable[[pb.CloudQueuedPublish], Awaitable[None]]


def _build_metrics_message(
    state: RuntimeState,
    snapshot: pb.DaemonMetrics,
    *,
    expiry_seconds: float,
) -> pb.CloudQueuedPublish:
    topic = topic_path(
        state.cloud_topic_prefix,
        Topic.SYSTEM,
        "metrics",
    )
    # [SIL-2] Direct Protobuf serialization without StructuredPayload overhead
    message = create_queued_publish(
        topic_name=topic,
        payload=snapshot.SerializeToString(),
        content_type=PROTOBUF_CONTENT_TYPE,
        message_expiry_interval=int(expiry_seconds),
        user_properties=(),
    )

    if snapshot.cloud_spool_degraded:
        message.user_properties.add(
            key=const.PROP_KEY_BRIDGE_SPOOL,
            value=snapshot.cloud_spool_failure_reason or const.PROP_VAL_UNKNOWN,
        )

    # Extra props for files
    if state.file_storage_limit_rejections > 0:
        message.user_properties.add(key=const.PROP_KEY_BRIDGE_FILES, value=const.PROP_VAL_QUOTA_BLOCKED)
    elif state.file_write_limit_rejections > 0:
        message.user_properties.add(key=const.PROP_KEY_BRIDGE_FILES, value=const.PROP_VAL_WRITE_LIMIT)

    message.user_properties.add(
        key=const.PROP_KEY_WATCHDOG_ENABLED,
        value=const.PROP_VAL_ENABLED_TRUE if snapshot.watchdog_enabled else const.PROP_VAL_ENABLED_FALSE,
    )
    if snapshot.watchdog_enabled:
        message.user_properties.add(key=const.PROP_KEY_WATCHDOG_INTERVAL, value=str(snapshot.watchdog_interval))

    return message


async def _emit_metrics_snapshot(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    *,
    expiry_seconds: float,
) -> None:
    snapshot = state.build_metrics_snapshot()
    await enqueue(
        _build_metrics_message(
            state,
            snapshot,
            expiry_seconds=expiry_seconds,
        )
    )


async def _emit_bridge_snapshot(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    flavor: str,
) -> None:
    try:
        snapshot = state.build_handshake_snapshot() if flavor == "handshake" else state.build_bridge_snapshot()
        await enqueue(
            _build_bridge_snapshot_message(
                state,
                flavor,
                snapshot,
            )
        )
    except asyncio.CancelledError:
        raise
    except (TypeError, ValueError, OSError) as e:
        logger.error(
            "Failed to publish bridge snapshot (serialization/IO): %s",
            e,
            flavor=flavor,
        )
    except AttributeError as e:
        logger.critical(
            "Unexpected error in bridge snapshot builder: %s",
            e,
            exc_info=True,
            flavor=flavor,
        )


async def publish_metrics(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    interval: float,
    *,
    min_interval: float = 5.0,
) -> None:
    """Publish runtime metrics to CLOUD at a fixed cadence.

    interval is sourced from RuntimeConfig.status_interval, which enforces
    status_interval > 0 natively at config-validation time, so no runtime guard is needed here.
    """

    tick_seconds = max(1, math.ceil(max(min_interval, interval)))
    expiry = float(tick_seconds * 2)

    async def _metrics_tick() -> None:
        try:
            await _emit_metrics_snapshot(state, enqueue, expiry_seconds=expiry)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as e:
            logger.error("Periodic metrics emit failed", error=str(e))

    try:
        while True:
            await _metrics_tick()
            await asyncio.sleep(tick_seconds)
    except asyncio.CancelledError:
        logger.info("Metrics publisher cancelled.")
        raise


async def publish_bridge_snapshots(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    *,
    summary_interval: float,
    handshake_interval: float,
    min_interval: float = 5.0,
) -> None:
    """Periodically publish bridge summary and handshake snapshots."""

    summary_seconds = max(1, math.ceil(max(min_interval, summary_interval))) if summary_interval > 0 else None
    handshake_seconds = max(1, math.ceil(max(min_interval, handshake_interval))) if handshake_interval > 0 else None

    if summary_seconds is None and handshake_seconds is None:
        logger.info("Bridge snapshot loops disabled; awaiting cancellation.")
        # Equivalent to waiting forever until cancelled
        await asyncio.Event().wait()
        return

    async with asyncio.TaskGroup() as tg:
        if summary_seconds is not None:

            async def _summary_loop() -> None:
                while True:
                    try:
                        await _emit_bridge_snapshot(state, enqueue, flavor="summary")
                    except asyncio.CancelledError:
                        raise
                    except (OSError, RuntimeError) as e:
                        logger.error("Bridge summary emit failed", error=str(e))
                    await asyncio.sleep(summary_seconds)

            tg.create_task(_summary_loop())

        if handshake_seconds is not None:

            async def _handshake_loop() -> None:
                while True:
                    try:
                        await _emit_bridge_snapshot(state, enqueue, flavor="handshake")
                    except asyncio.CancelledError:
                        raise
                    except (OSError, RuntimeError) as e:
                        logger.error("Bridge handshake emit failed", error=str(e))
                    await asyncio.sleep(handshake_seconds)

            tg.create_task(_handshake_loop())


def _build_bridge_snapshot_message(
    state: RuntimeState,
    flavor: str,
    snapshot: Any,
) -> pb.CloudQueuedPublish:
    segments: Sequence[str] = (
        ("bridge", "handshake", "value") if flavor == "handshake" else ("bridge", "summary", "value")
    )
    topic = topic_path(
        state.cloud_topic_prefix,
        Topic.SYSTEM,
        *segments,
    )
    return create_queued_publish(
        topic_name=topic,
        payload=snapshot.SerializeToString(),
        content_type=PROTOBUF_CONTENT_TYPE,
        message_expiry_interval=const.BRIDGE_SNAPSHOT_EXPIRY_SECONDS,
        user_properties=((const.PROP_KEY_BRIDGE_SNAPSHOT, flavor),),
    )
