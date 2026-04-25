"""MQTT utilities and core link management (SIL-2)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import msgspec
import structlog
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from mcubridge.protocol.structures import QueuedPublish
from . import spool_manager

if TYPE_CHECKING:
    from aiomqtt.message import Message
    from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.mqtt")


def build_mqtt_connect_properties() -> Properties:
    """Build standard MQTT v5 connection properties."""
    properties = Properties(PacketTypes.CONNECT)
    properties.SessionExpiryInterval = 3600
    properties.MaximumPacketSize = 65535
    return properties


async def enqueue_publish(
    state: RuntimeState,
    message: QueuedPublish,
    *,
    reply_context: Message | None = None,
) -> None:
    """
    Enqueues an MQTT message for publishing with an overflow dropping strategy.

    [SIL-2] Direct implementation without intermediary transport wrappers.
    """
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
        user_properties = list(message_to_queue.user_properties)
        user_properties.append(("bridge-request-topic", origin_topic))
        message_to_queue = msgspec.structs.replace(
            message_to_queue, user_properties=tuple(user_properties)
        )

    try:
        state.mqtt_publish_queue.put_nowait(message_to_queue)
    except asyncio.QueueFull:
        # Dropping strategy: discard oldest, spool it, and insert new
        try:
            dropped = state.mqtt_publish_queue.get_nowait()
            state.mqtt_publish_queue.task_done()

            # Record metrics directly in state
            state.mqtt_drop_counts[dropped.topic_name] = (
                state.mqtt_drop_counts.get(dropped.topic_name, 0) + 1
            )
            state.mqtt_dropped_messages += 1
            state.metrics.mqtt_messages_dropped.inc()

            # Stash in spool (background safe)
            await spool_manager.stash_message(state, dropped)

            # Insert new message
            state.mqtt_publish_queue.put_nowait(message_to_queue)

            logger.warning(
                "MQTT publish queue saturated; dropped oldest message from topic=%s",
                dropped.topic_name,
            )
        except asyncio.QueueEmpty:
            state.mqtt_publish_queue.put_nowait(message_to_queue)


async def atomic_publish(
    state: RuntimeState,
    topic: str,
    payload: bytes | str,
    *,
    qos: int = 0,
    retain: bool = False,
    expiry: int | None = None,
    properties: tuple[tuple[str, str], ...] = (),
    content_type: str | None = None,
    reply_to: Message | None = None,
) -> None:
    """Helper to publish an MQTT message directly via the state queue."""
    if isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    else:
        payload_bytes = payload

    message = QueuedPublish(
        topic_name=topic,
        payload=payload_bytes,
        qos=qos,
        retain=retain,
        content_type=content_type,
        message_expiry_interval=expiry,
        user_properties=tuple(properties or ()),
    )
    await enqueue_publish(state, message, reply_context=reply_to)
