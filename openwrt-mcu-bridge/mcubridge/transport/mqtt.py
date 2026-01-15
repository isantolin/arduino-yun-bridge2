"""MQTT transport helpers for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path
from typing import TYPE_CHECKING

import aiomqtt
# [REQ-PY3.13] Paho 2.x is used by aiomqtt internally, but aiomqtt > 2.0 handles the callback versioning.

from mcubridge.common import build_mqtt_connect_properties, build_mqtt_properties
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
            context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH, cafile=config.mqtt_cafile
            )
        else:
            # Use system trust store.
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

        context.minimum_version = MQTT_TLS_MIN_VERSION

        # Equivalent to mosquitto_{pub,sub} --insecure: disable hostname verification
        # (useful when connecting via IP while the certificate CN/SAN is a DNS name).
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
        except Exception as exc:
            logger.critical("CRITICAL: Unexpected error in MQTT publisher loop: %s", exc, exc_info=True)
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
            try:
                await service.handle_mqtt_message(message)
            except Exception as e:
                logger.exception("CRITICAL: Error processing MQTT topic %s: %s", topic, e)
    except asyncio.CancelledError:
        pass  # Clean exit
    except aiomqtt.MqttError as exc:
        logger.warning("MQTT subscriber loop interrupted: %s", exc)
        raise


async def mqtt_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
) -> None:
    tls_context = _configure_tls(config)
    reconnect_delay = max(1, config.reconnect_delay)

    while True:
        try:
            connect_props = build_mqtt_connect_properties()

            # [FIX-10/10] aiomqtt 2.x wraps Paho 2.x and handles CallbackAPIVersion internally.
            # Passing callback_api_version explicitly is not supported in aiomqtt constructor.
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

                # Subscription List (no closures/lambdas; always read prefix from state)
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

        except* aiomqtt.MqttError as exc_group:
            for exc in exc_group.exceptions:
                logger.error("MQTT connection failed: %s", exc)
        except* (OSError, asyncio.TimeoutError) as exc_group:
            for exc in exc_group.exceptions:
                logger.error("Network error: %s", exc)
        except* asyncio.CancelledError:
            logger.info("MQTT task stopping.")
            raise
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                logger.critical("CRITICAL: Unexpected MQTT error: %s", exc, exc_info=exc)

        logger.info("Reconnecting MQTT in %ds...", reconnect_delay)
        await asyncio.sleep(reconnect_delay)
