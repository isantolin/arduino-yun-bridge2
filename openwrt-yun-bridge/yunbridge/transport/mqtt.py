"""MQTT transport helpers for the Yun Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
import ssl

from aiomqtt import Client as MqttClient, MqttError, ProtocolVersion

from yunbridge.common import (
    apply_mqtt_connect_properties,
    build_mqtt_properties,
)
from yunbridge.config.settings import RuntimeConfig
from yunbridge.config.tls import build_tls_context, resolve_tls_material
from yunbridge.protocol import Topic, topic_path
from yunbridge.services.runtime import BridgeService
from yunbridge.state.context import RuntimeState

logger = logging.getLogger("yunbridge")


async def _mqtt_publisher_loop(
    client: MqttClient,
    state: RuntimeState,
) -> None:
    while True:
        await state.flush_mqtt_spool()
        message_to_publish = await state.mqtt_publish_queue.get()
        topic_name = message_to_publish.topic_name

        props = build_mqtt_properties(message_to_publish)

        try:
            await client.publish(
                topic_name,
                message_to_publish.payload,
                qos=int(message_to_publish.qos),
                retain=message_to_publish.retain,
                properties=props,
            )
        except asyncio.CancelledError:
            logger.info("MQTT publisher loop cancelled.")
            try:
                state.mqtt_publish_queue.put_nowait(message_to_publish)
            except asyncio.QueueFull:
                logger.debug(
                    "MQTT publish queue full while cancelling; dropping %s",
                    topic_name,
                )
            raise
        except MqttError as exc:
            logger.warning(
                "MQTT publish failed for %s; broker unavailable (%s)",
                topic_name,
                exc,
            )
            try:
                state.mqtt_publish_queue.put_nowait(message_to_publish)
            except asyncio.QueueFull:
                logger.error(
                    "MQTT publish queue full; dropping message for %s",
                    topic_name,
                )
            raise
        except Exception:
            logger.exception(
                "Failed to publish MQTT message for topic %s",
                topic_name,
            )
            raise
        finally:
            state.mqtt_publish_queue.task_done()
            await state.flush_mqtt_spool()


async def _mqtt_subscriber_loop(
    client: MqttClient,
    service: BridgeService,
) -> None:
    try:
        async with client.messages() as stream:
            async for message in stream:
                topic = str(message.topic)
                if not topic:
                    continue
                try:
                    await service.handle_mqtt_message(message)
                except Exception:
                    logger.exception(
                        "Error processing MQTT topic %s",
                        topic,
                    )
    except asyncio.CancelledError:
        logger.info("MQTT subscriber loop cancelled.")
        raise
    except MqttError as exc:
        logger.warning("MQTT subscriber loop stopped: %s", exc)
        raise


def build_mqtt_tls_context(config: RuntimeConfig) -> ssl.SSLContext | None:
    if not config.tls_enabled:
        return None

    try:
        material = resolve_tls_material(config)
        context = build_tls_context(material)
        if material.certfile and material.keyfile:
            logger.info("Using MQTT TLS with client certificate authentication.")
        else:
            logger.info("Using MQTT TLS with CA verification only.")
        return context
    except (ssl.SSLError, FileNotFoundError, RuntimeError) as exc:
        message = f"Failed to create TLS context: {exc}"
        raise RuntimeError(message) from exc


async def mqtt_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
    tls_context: ssl.SSLContext | None,
) -> None:
    reconnect_delay = max(1, config.reconnect_delay)

    while True:
        try:
            async with MqttClient(
                hostname=config.mqtt_host,
                port=config.mqtt_port,
                username=config.mqtt_user or None,
                password=config.mqtt_pass or None,
                tls_context=tls_context,
                logger=logging.getLogger("yunbridge.mqtt.client"),
                protocol=ProtocolVersion.V5,
                clean_session=None,
            ) as client:
                apply_mqtt_connect_properties(client)
                logger.info("Connected to MQTT broker.")

                prefix = state.mqtt_topic_prefix

                def _sub_path(top: Topic | str, *segs: str) -> str:
                    return topic_path(prefix, top, *segs)

                topics = [
                    (_sub_path(Topic.DIGITAL, "+", "mode"), 0),
                    (_sub_path(Topic.DIGITAL, "+", "read"), 0),
                    (_sub_path(Topic.DIGITAL, "+"), 0),
                    (_sub_path(Topic.ANALOG, "+", "read"), 0),
                    (_sub_path(Topic.ANALOG, "+"), 0),
                    (_sub_path(Topic.CONSOLE, "in"), 0),
                    (_sub_path(Topic.DATASTORE, "put", "#"), 0),
                    (_sub_path(Topic.DATASTORE, "get", "#"), 0),
                    (_sub_path(Topic.MAILBOX, "write"), 0),
                    (_sub_path(Topic.MAILBOX, "read"), 0),
                    (_sub_path(Topic.SHELL, "run"), 0),
                    (_sub_path(Topic.SHELL, "run_async"), 0),
                    (_sub_path(Topic.SHELL, "poll", "#"), 0),
                    (_sub_path(Topic.SHELL, "kill", "#"), 0),
                    (_sub_path(Topic.SYSTEM, "free_memory", "get"), 0),
                    (_sub_path(Topic.SYSTEM, "version", "get"), 0),
                    (_sub_path(Topic.FILE, "write", "#"), 0),
                    (_sub_path(Topic.FILE, "read", "#"), 0),
                    (_sub_path(Topic.FILE, "remove", "#"), 0),
                ]

                for topic, qos in topics:
                    await client.subscribe(topic, qos=qos)

                logger.info("Subscribed to %d MQTT topics.", len(topics))

                async with asyncio.TaskGroup() as task_group:
                    task_group.create_task(_mqtt_publisher_loop(client, state))
                    task_group.create_task(_mqtt_subscriber_loop(client, service))

        except* MqttError as exc_group:
            for exc in exc_group.exceptions:
                logger.error("MQTT error: %s", exc)
        except* (OSError, asyncio.TimeoutError) as exc_group:
            for exc in exc_group.exceptions:
                logger.error("MQTT connection error: %s", exc)
        except* asyncio.CancelledError:
            logger.info("MQTT task cancelled.")
            raise
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                logger.critical(
                    "Unhandled exception in mqtt_task",
                    exc_info=exc,
                )
        logger.warning(
            "Waiting %d seconds before MQTT reconnect...",
            reconnect_delay,
        )
        try:
            await asyncio.sleep(reconnect_delay)
        except asyncio.CancelledError:
            logger.info("MQTT task cancelled during backoff.")
            raise


__all__ = [
    "build_mqtt_tls_context",
    "mqtt_task",
]
