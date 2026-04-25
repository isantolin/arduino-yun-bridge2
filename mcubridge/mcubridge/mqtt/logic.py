"""MQTT business logic for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiomqtt
import structlog
import tenacity

from ..protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS
from ..protocol.topics import topic_path

if TYPE_CHECKING:
    from ..services.runtime import BridgeService
    from ..state.context import RuntimeState

logger = structlog.get_logger("mcubridge.mqtt")


async def mqtt_publisher_loop(
    client: aiomqtt.Client,
    state: RuntimeState,
) -> None:
    """Publishes messages from the internal queue to the MQTT broker (Zero-Wrapper)."""
    try:
        while True:
            # 1. Flush spool before processing new messages (Direct delegation)
            # This will be called from a spool helper in a future step or here directly.
            # For now, we assume state handles its own spool logic or we call it.

            # Wait for next message from the state queue
            message = await state.mqtt_publish_queue.get()

            # Pre-calculate for reliability
            topic_name = message.topic_name
            props = message.to_paho_properties()
            payload = message.payload
            qos = int(message.qos)
            retain = message.retain

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[HEXDUMP] MQTT PUB > %s: %s", topic_name, payload.hex(" ").upper()
                )

            # [SIL-2] Direct use of tenacity for reliable publishing without class wrapping
            retryer = tenacity.AsyncRetrying(
                wait=tenacity.wait_exponential(multiplier=0.1, max=10),
                retry=tenacity.retry_if_exception_type(aiomqtt.MqttError),
                before_sleep=tenacity.before_sleep_log(logger, logging.DEBUG),
                reraise=True,
            )

            published = False
            try:
                async for attempt in retryer:
                    with attempt:
                        await client.publish(
                            topic_name,
                            payload,
                            qos=qos,
                            retain=retain,
                            properties=props,
                        )
                state.mqtt_messages_published += 1
                state.metrics.mqtt_messages_published.inc()
                published = True
            except aiomqtt.MqttError as exc:
                logger.warning("MQTT persistent publish failure: %s", exc)
            except Exception as exc:
                logger.error("Unexpected error in MQTT publisher: %s", exc)
            finally:
                if not published:
                    # Spooling logic will be integrated here or in state
                    pass
                state.mqtt_publish_queue.task_done()

    except asyncio.CancelledError:
        logger.debug("MQTT publisher loop cancelled.")
        raise


async def mqtt_subscriber_loop(
    client: aiomqtt.Client,
    service: BridgeService,
) -> None:
    """Subscribes and dispatches messages to the BridgeService."""
    # 1. Initial Subscriptions
    topics = [
        (topic_path(service.state.mqtt_topic_prefix, t, *s), int(q))
        for t, s, q in MQTT_COMMAND_SUBSCRIPTIONS
    ]
    await client.subscribe(topics)
    logger.info("Subscribed to %d command topics.", len(topics))

    try:
        async for message in client.messages:
            try:
                topic_str = str(message.topic)
                if not topic_str:
                    continue

                await service.handle_mqtt_message(message)
            except Exception as e:
                logger.error("Error processing MQTT message: %s", e)
    except asyncio.CancelledError:
        raise
    except aiomqtt.MqttError as exc:
        logger.warning("MQTT subscriber loop interrupted: %s", exc)
        raise
