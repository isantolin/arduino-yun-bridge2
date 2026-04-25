"""Simplified MQTT connection orchestration (Zero-Wrapper)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiomqtt
import structlog
import tenacity

from ..mqtt import build_mqtt_connect_properties
from ..protocol.topics import topic_path
from ..protocol.protocol import Topic
from ..mqtt.logic import mqtt_publisher_loop, mqtt_subscriber_loop

if TYPE_CHECKING:
    from ..config.settings import RuntimeConfig
    from ..state.context import RuntimeState
    from ..services.runtime import BridgeService

logger = structlog.get_logger("mcubridge.mqtt")


async def run_mqtt_client(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
) -> None:
    """Main MQTT run loop with direct aiomqtt.Client usage."""
    if not config.mqtt_enabled:
        return

    tls_context = config.get_ssl_context()
    reconnect_delay = max(1, config.reconnect_delay)

    # [SIL-2] Reconnection logic using tenacity at the connection level
    retryer = tenacity.AsyncRetrying(
        wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60),
        retry=tenacity.retry_if_exception_type(
            (aiomqtt.MqttError, OSError, asyncio.TimeoutError)
        ),
        before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )

    async for attempt in retryer:
        with attempt:
            await _connect_and_run(config, state, service, tls_context)


async def _connect_and_run(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
    tls_context: Any,
) -> None:
    """Single connection session logic."""
    connect_props = build_mqtt_connect_properties()
    will_topic = topic_path(state.mqtt_topic_prefix, Topic.SYSTEM, "status")
    will_payload = b'{"status": "offline", "reason": "unexpected_disconnect"}'
    will = aiomqtt.Will(topic=will_topic, payload=will_payload, qos=1, retain=True)

    async with aiomqtt.Client(
        hostname=config.mqtt_host,
        port=config.mqtt_port,
        username=config.mqtt_user or None,
        password=config.mqtt_pass or None,
        tls_context=tls_context,
        protocol=aiomqtt.ProtocolVersion.V5,
        will=will,
        properties=connect_props,
        logger=logging.getLogger("mcubridge.mqtt.client"),
    ) as client:
        logger.info("Connected to MQTT broker.")

        # Publish online status
        await client.publish(will_topic, b'{"status": "online"}', qos=1, retain=True)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(mqtt_publisher_loop(client, state))
            tg.create_task(mqtt_subscriber_loop(client, service))
