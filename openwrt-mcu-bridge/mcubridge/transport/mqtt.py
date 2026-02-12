"""MQTT transport helpers for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiomqtt
import tenacity

from mcubridge.mqtt import build_mqtt_connect_properties, build_mqtt_properties
from mcubridge.util.mqtt_helper import configure_tls_context
from mcubridge.util import log_hexdump
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import topic_path
from mcubridge.protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS
from mcubridge.state.context import RuntimeState

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

logger = logging.getLogger("mcubridge")


async def _mqtt_publisher_loop(
    state: RuntimeState,
    client: aiomqtt.Client,
) -> None:
    while True:
        # [OPTIMIZATION] Flush spool before processing new messages
        await state.flush_mqtt_spool()
        message = await state.mqtt_publish_queue.get()
        topic_name = message.topic_name
        props = build_mqtt_properties(message)

        if logger.isEnabledFor(logging.DEBUG):
            log_hexdump(logger, logging.DEBUG, f"MQTT PUB > {topic_name}", message.payload)

        try:
            await client.publish(
                topic_name,
                message.payload,
                qos=int(message.qos),
                retain=message.retain,
                properties=props,
            )
        except asyncio.CancelledError:
            logger.debug("MQTT publisher loop cancelled.")
            try:
                state.mqtt_publish_queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("MQTT queue full during shutdown; message dropped.")
            raise
        except aiomqtt.MqttError as exc:
            logger.warning("MQTT publish failed (%s); requeuing.", exc)
            try:
                state.mqtt_publish_queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.error("MQTT spool full; message dropped.")
            raise
        finally:
            state.mqtt_publish_queue.task_done()


async def _mqtt_subscriber_loop(
    service: BridgeService,
    client: aiomqtt.Client,
) -> None:
    try:
        async for message in client.messages:
            topic = str(message.topic)
            if not topic:
                continue

            if logger.isEnabledFor(logging.DEBUG):
                # aiomqtt 2.x: payload is always bytes | bytearray
                payload_bytes = bytes(message.payload) if message.payload else b""
                log_hexdump(logger, logging.DEBUG, f"MQTT SUB < {topic}", payload_bytes)

            try:
                await service.handle_mqtt_message(message)
            except (ValueError, TypeError, AttributeError, RuntimeError, KeyError) as e:
                logger.exception("CRITICAL: Error processing MQTT topic %s: %s", topic, e)
    except asyncio.CancelledError:
        pass  # Clean exit
    except aiomqtt.MqttError as exc:
        logger.warning("MQTT subscriber loop interrupted: %s", exc)
        raise


def _log_retry_attempt(retry_state: tenacity.RetryCallState) -> None:
    if retry_state.attempt_number > 1:
        logger.info(
            "Reconnecting MQTT (attempt %d, next wait %.2fs)...",
            retry_state.attempt_number,
            retry_state.next_action.sleep if retry_state.next_action else 0,
        )


async def mqtt_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
) -> None:
    tls_context = configure_tls_context(config)
    reconnect_delay = max(1, config.reconnect_delay)

    retryer = tenacity.AsyncRetrying(
        wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60) + tenacity.wait_random(0, 2),
        retry=tenacity.retry_if_exception_type((aiomqtt.MqttError, OSError, asyncio.TimeoutError)),
        before_sleep=_log_retry_attempt,
        reraise=True,
    )

    async for attempt in retryer:
        with attempt:
            try:
                connect_props = build_mqtt_connect_properties()

                # [SIL-2] Warn if connecting without authentication
                if not config.mqtt_user:
                    logger.warning(
                        "MQTT connecting without authentication (anonymous); "
                        "consider setting mqtt_user/mqtt_pass for production"
                    )

                async with aiomqtt.Client(
                    hostname=config.mqtt_host,
                    port=config.mqtt_port,
                    username=config.mqtt_user or None,
                    password=config.mqtt_pass or None,
                    tls_context=tls_context,
                    logger=logging.getLogger("mcubridge.mqtt.client"),
                    protocol=aiomqtt.ProtocolVersion.V5,
                    clean_session=None,
                    properties=connect_props,
                ) as client:
                    logger.info("Connected to MQTT broker (Paho v2/MQTTv5).")

                    # Subscription List
                    topics: list[tuple[str, int]] = []
                    for topic_enum, segments, qos in MQTT_COMMAND_SUBSCRIPTIONS:
                        topics.append(
                            (
                                topic_path(state.mqtt_topic_prefix, topic_enum, *segments),
                                int(qos),
                            )
                        )

                    for topic, qos in topics:
                        await client.subscribe(topic, qos=qos)

                    logger.info("Subscribed to %d command topics.", len(topics))

                    async with asyncio.TaskGroup() as task_group:
                        task_group.create_task(_mqtt_publisher_loop(state, client))
                        task_group.create_task(_mqtt_subscriber_loop(service, client))

            except* asyncio.CancelledError:
                logger.info("MQTT task stopping.")
                raise asyncio.CancelledError()
            except* (aiomqtt.MqttError, OSError, asyncio.TimeoutError) as exc_group:
                for exc in exc_group.exceptions:
                    logger.error("MQTT connection error: %s", exc)
                if len(exc_group.exceptions) == 1:
                    raise exc_group.exceptions[0]
                raise
