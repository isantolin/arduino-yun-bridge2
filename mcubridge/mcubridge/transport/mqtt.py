"""MQTT core link management (SIL-2)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import aiomqtt
import structlog
import tenacity

from mcubridge.mqtt import (
    atomic_publish,
    build_mqtt_connect_properties,
    enqueue_publish,
    spool_manager,
)
from mcubridge.protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS, Topic
from mcubridge.protocol.topics import topic_path

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import RuntimeState
    from mcubridge.protocol.structures import QueuedPublish

logger = structlog.get_logger("mcubridge.mqtt")


class MqttTransport:
    """[SIL-2] Re-factored MQTT transport delegating to core utilities."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.service: BridgeService | None = None

    def set_service(self, service: BridgeService) -> None:
        self.service = service

    def configure_spool(self, directory: str, limit: int) -> None:
        self.state.mqtt_spool_dir = directory
        self.state.mqtt_spool_limit = limit

    def initialize_spool(self) -> None:
        spool_manager.initialize_spool(self.state)

    async def ensure_spool(self) -> bool:
        return await spool_manager.ensure_spool(self.state)

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        reply_context: aiomqtt.message.Message | None = None,
    ) -> None:
        await enqueue_publish(self.state, message, reply_context=reply_context)

    async def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        """Shim for legacy tests."""
        return await spool_manager.stash_message(self.state, message)

    async def flush_mqtt_spool(self) -> None:
        """Shim for legacy tests."""
        await spool_manager.flush_spool(self.state, None)

    def _disable_mqtt_spool(self, reason: str, schedule_retry: bool = True) -> None:
        """Shim for legacy tests."""
        spool_manager._disable_spool(self.state, reason, schedule_retry)

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: aiomqtt.message.Message | None = None,
    ) -> None:
        """Deprecated: wrapper around mcubridge.mqtt.atomic_publish."""
        await atomic_publish(
            self.state,
            topic,
            payload,
            qos=qos,
            retain=retain,
            expiry=expiry,
            properties=properties,
            content_type=content_type,
            reply_to=reply_to,
        )

    async def run(self) -> None:
        """Main run loop with reconnection logic."""
        if not self.config.mqtt_enabled:
            logger.info("MQTT transport is DISABLED in configuration.")
            return

        tls_context = self.config.get_ssl_context()
        reconnect_delay = max(1, self.config.reconnect_delay)
        _log_cb = tenacity.before_sleep_log(logger, logging.WARNING)

        def _before_sleep(retry_state: tenacity.RetryCallState) -> None:
            _log_cb(retry_state)
            self.state.metrics.retries.labels(component="mqtt_connect").inc()

        def _retry_predicate(retry_state: tenacity.RetryCallState) -> bool:
            if not retry_state.outcome or not retry_state.outcome.failed:
                return False
            exc = retry_state.outcome.exception()
            if not exc:
                return False
            retryable = (aiomqtt.MqttError, OSError, asyncio.TimeoutError)

            def _is_retryable(e: Any) -> bool:
                if isinstance(e, retryable):
                    return True
                if isinstance(e, ExceptionGroup):
                    return any(_is_retryable(se) for se in cast(Any, e).exceptions)
                return False

            return _is_retryable(exc)

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60)
            + tenacity.wait_random(0, 2),
            retry=_retry_predicate,
            before_sleep=_before_sleep,
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    await self._connect_session(tls_context)
        except asyncio.CancelledError:
            logger.info("MQTT transport stopping.")
            raise
        except Exception as exc:
            logger.critical("MQTT transport fatal error: %s", exc)
            raise

    async def _connect_session(self, tls_context: Any) -> None:
        connect_props = build_mqtt_connect_properties()
        will_topic = topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, "status")
        will_payload = b'{"status": "offline", "reason": "unexpected_disconnect"}'
        will = aiomqtt.Will(topic=will_topic, payload=will_payload, qos=1, retain=True)

        async with aiomqtt.Client(
            hostname=self.config.mqtt_host,
            port=self.config.mqtt_port,
            username=self.config.mqtt_user or None,
            password=self.config.mqtt_pass or None,
            tls_context=tls_context,
            logger=logging.getLogger("mcubridge.mqtt.client"),
            protocol=aiomqtt.ProtocolVersion.V5,
            clean_session=None,
            will=will,
            properties=connect_props,
        ) as client:
            logger.info("Connected to MQTT broker.")
            topics = [
                (topic_path(self.state.mqtt_topic_prefix, t, *s), int(q))
                for t, s, q in MQTT_COMMAND_SUBSCRIPTIONS
            ]
            await client.subscribe(topics)
            await client.publish(
                will_topic, b'{"status": "online"}', qos=1, retain=True
            )

            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._publisher_loop(client))
                tg.create_task(self._subscriber_loop(client))

    async def _publisher_loop(self, client: aiomqtt.Client) -> None:
        try:
            while True:
                await spool_manager.flush_spool(self.state, client.publish)
                message = await self.state.mqtt_publish_queue.get()
                try:
                    await client.publish(
                        message.topic_name,
                        message.payload,
                        qos=int(message.qos),
                        retain=message.retain,
                        properties=message.to_paho_properties(),
                    )
                    self.state.mqtt_messages_published += 1
                    self.state.metrics.mqtt_messages_published.inc()
                except aiomqtt.MqttError as exc:
                    logger.warning("MQTT publish failure: %s", exc)
                    await spool_manager.stash_message(self.state, message)
                except Exception as exc:
                    logger.error("Unexpected MQTT error: %s", exc)
                    await spool_manager.stash_message(self.state, message)
                finally:
                    self.state.mqtt_publish_queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _subscriber_loop(self, client: aiomqtt.Client) -> None:
        try:
            async for message in client.messages:
                if self.service:
                    await self.service.handle_mqtt_message(message)
        except asyncio.CancelledError:
            raise
        except aiomqtt.MqttError as exc:
            logger.warning("MQTT subscriber interrupted: %s", exc)
            raise
