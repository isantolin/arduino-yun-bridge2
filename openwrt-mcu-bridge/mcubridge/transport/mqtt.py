"""MQTT transport helpers for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path
from typing import TYPE_CHECKING

import aiomqtt
import tenacity

from mcubridge.common import build_mqtt_connect_properties, build_mqtt_properties, log_hexdump
from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import MQTT_TLS_MIN_VERSION
from mcubridge.protocol import topic_path
from mcubridge.rpc.protocol import MQTT_COMMAND_SUBSCRIPTIONS
from mcubridge.state.context import RuntimeState

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

logger = logging.getLogger("mcubridge")


def _configure_tls(config: RuntimeConfig) -> ssl.SSLContext | None:
    if not config.tls_enabled:
        return None

    try:
        if config.mqtt_cafile:
            if not Path(config.mqtt_cafile).exists():
                raise RuntimeError(f"MQTT TLS CA file missing: {config.mqtt_cafile}")
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=config.mqtt_cafile)
        else:
            # Use system trust store.
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

        context.minimum_version = MQTT_TLS_MIN_VERSION

        # Equivalent to mosquitto_{pub,sub} --insecure: disable hostname verification
        if getattr(config, "mqtt_tls_insecure", False):
            context.check_hostname = False

        if config.mqtt_certfile and config.mqtt_keyfile:
            context.load_cert_chain(config.mqtt_certfile, config.mqtt_keyfile)

        return context
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise RuntimeError(f"TLS setup failed: {exc}") from exc


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
                # Ensure payload is bytes for hexdump.
                payload_bytes: bytes = b""
                if isinstance(message.payload, (bytes, bytearray)):
                    payload_bytes = bytes(message.payload)
                elif isinstance(message.payload, str):
                    payload_bytes = message.payload.encode("utf-8")
                elif message.payload is not None:
                    # int/float fallback
                    payload_bytes = str(message.payload).encode("ascii")

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
    tls_context = _configure_tls(config)
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
