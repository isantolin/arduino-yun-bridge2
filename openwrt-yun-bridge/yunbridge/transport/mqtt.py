"""MQTT transport helpers for the Yun Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

import aiomqtt
# [REQ-PY3.13] Paho 2.x is used by aiomqtt internally, but aiomqtt > 2.0 handles the callback versioning.

from yunbridge.common import build_mqtt_connect_properties, build_mqtt_properties
from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import MQTT_TLS_MIN_VERSION
from yunbridge.protocol import Action, Topic, topic_path
from yunbridge.services.runtime import BridgeService
from yunbridge.state.context import RuntimeState

logger = logging.getLogger("yunbridge")


def _configure_tls(config: RuntimeConfig) -> ssl.SSLContext | None:
    if not config.tls_enabled:
        return None

    if not config.mqtt_cafile or not Path(config.mqtt_cafile).exists():
        raise RuntimeError(f"MQTT TLS CA file missing: {config.mqtt_cafile}")

    try:
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=config.mqtt_cafile
        )
        context.minimum_version = MQTT_TLS_MIN_VERSION
        if config.mqtt_certfile and config.mqtt_keyfile:
            context.load_cert_chain(config.mqtt_certfile, config.mqtt_keyfile)

        return context
    except Exception as exc:
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
        except Exception:
            logger.exception("Critical error in MQTT publisher.")
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
            except Exception:
                logger.exception("Error processing MQTT topic %s", topic)
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
                logger=logging.getLogger("yunbridge.mqtt.client"),
                protocol=aiomqtt.ProtocolVersion.V5,
                clean_session=None,
                properties=connect_props,
            ) as client:
                logger.info("Connected to MQTT broker (Paho v2/MQTTv5).")

                # Subscription List (no closures/lambdas; always read prefix from state)
                topics = [
                    (
                        topic_path(
                            state.mqtt_topic_prefix,
                            Topic.DIGITAL,
                            "+",
                            Action.PIN_MODE,
                        ),
                        0,
                    ),
                    (
                        topic_path(
                            state.mqtt_topic_prefix,
                            Topic.DIGITAL,
                            "+",
                            Action.PIN_READ,
                        ),
                        0,
                    ),
                    (topic_path(state.mqtt_topic_prefix, Topic.DIGITAL, "+"), 0),
                    (
                        topic_path(
                            state.mqtt_topic_prefix,
                            Topic.ANALOG,
                            "+",
                            Action.PIN_READ,
                        ),
                        0,
                    ),
                    (topic_path(state.mqtt_topic_prefix, Topic.ANALOG, "+"), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.CONSOLE, Action.CONSOLE_IN), 0),
                    (
                        topic_path(
                            state.mqtt_topic_prefix,
                            Topic.DATASTORE,
                            Action.DATASTORE_PUT,
                            "#",
                        ),
                        0,
                    ),
                    (
                        topic_path(
                            state.mqtt_topic_prefix,
                            Topic.DATASTORE,
                            Action.DATASTORE_GET,
                            "#",
                        ),
                        0,
                    ),
                    (topic_path(state.mqtt_topic_prefix, Topic.MAILBOX, Action.MAILBOX_WRITE), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.MAILBOX, Action.MAILBOX_READ), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.SHELL, Action.SHELL_RUN), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.SHELL, Action.SHELL_RUN_ASYNC), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.SHELL, Action.SHELL_POLL, "#"), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.SHELL, Action.SHELL_KILL, "#"), 0),
                    (
                        topic_path(
                            state.mqtt_topic_prefix,
                            Topic.SYSTEM,
                            Action.SYSTEM_FREE_MEMORY,
                            Action.SYSTEM_GET,
                        ),
                        0,
                    ),
                    (
                        topic_path(
                            state.mqtt_topic_prefix,
                            Topic.SYSTEM,
                            Action.SYSTEM_VERSION,
                            Action.SYSTEM_GET,
                        ),
                        0,
                    ),
                    (topic_path(state.mqtt_topic_prefix, Topic.FILE, Action.FILE_WRITE, "#"), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.FILE, Action.FILE_READ, "#"), 0),
                    (topic_path(state.mqtt_topic_prefix, Topic.FILE, Action.FILE_REMOVE, "#"), 0),
                ]

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
                logger.critical("Unexpected MQTT error", exc_info=exc)

        logger.info("Reconnecting MQTT in %ds...", reconnect_delay)
        await asyncio.sleep(reconnect_delay)
