"""Periodic metrics publisher for Yun Bridge."""
from __future__ import annotations

import asyncio
import json
import logging
from .protocol.topics import Topic, topic_path
from .mqtt import PublishableMessage
from .state.context import RuntimeState

logger = logging.getLogger("yunbridge.metrics")


async def publish_metrics(
    state: RuntimeState,
    enqueue,
    interval: float,
    *,
    min_interval: float = 5.0,
) -> None:
    """Publish runtime metrics to MQTT at a fixed cadence."""

    tick = max(min_interval, interval)
    while True:
        try:
            snapshot = state.build_metrics_snapshot()
            payload = json.dumps(snapshot).encode("utf-8")
            topic = topic_path(
                state.mqtt_topic_prefix,
                Topic.SYSTEM,
                "metrics",
            )
            message = (
                PublishableMessage(topic_name=topic, payload=payload)
                .with_content_type("application/json")
                .with_message_expiry(int(tick * 2))
            )
            await enqueue(message)
        except asyncio.CancelledError:
            logger.info("Metrics publisher cancelled.")
            raise
        except Exception:
            logger.exception("Failed to publish metrics payload")
        await asyncio.sleep(tick)


__all__ = ["publish_metrics"]
