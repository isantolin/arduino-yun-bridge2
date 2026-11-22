"""Periodic metrics publisher for MCU Bridge (Pure Telemetry Push, SIL-2)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from .protocol import cloud_gateway_pb2
from .protocol.protocol import get_topic_for_message
from .state.context import RuntimeState

logger = structlog.get_logger("mcubridge.metrics")

PublishEnqueue = Callable[[cloud_gateway_pb2.CloudQueuedPublish], Awaitable[None]]


def _build_telemetry_message(
    state: RuntimeState,
    snapshot: Any,
) -> cloud_gateway_pb2.CloudQueuedPublish:
    topic = get_topic_for_message(state.cloud_topic_prefix, snapshot) or ""
    return cloud_gateway_pb2.CloudQueuedPublish(
        topic_name=topic,
        payload=snapshot.SerializeToString(),
    )


async def _emit_metrics_snapshot(
    state: RuntimeState,
    enqueue: PublishEnqueue,
) -> None:
    snapshot = state.build_metrics_snapshot()
    await enqueue(_build_telemetry_message(state, snapshot))


async def emit_bridge_snapshot(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    flavor: str,
) -> None:
    try:
        snapshot = state.build_handshake_snapshot() if flavor == "handshake" else state.build_bridge_snapshot()
        await enqueue(_build_telemetry_message(state, snapshot))
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

    async def _metrics_tick() -> None:
        try:
            await _emit_metrics_snapshot(state, enqueue)
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

    async def _snapshot_loop(flavor: str, interval: int) -> None:
        while True:
            try:
                await emit_bridge_snapshot(state, enqueue, flavor=flavor)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError) as e:
                logger.error(f"Bridge {flavor} emit failed", error=str(e))
            await asyncio.sleep(interval)

    async with asyncio.TaskGroup() as tg:
        if summary_seconds is not None:
            tg.create_task(_snapshot_loop("summary", summary_seconds))
        if handshake_seconds is not None:
            tg.create_task(_snapshot_loop("handshake", handshake_seconds))
