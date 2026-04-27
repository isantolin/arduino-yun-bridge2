"""Simplified and robust MQTT transport (SIL-2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, cast

import aiomqtt
import msgspec
import structlog
import tenacity

from mcubridge.mqtt import build_mqtt_connect_properties
from mcubridge.mqtt.spool import MQTTPublishSpool, MQTTSpoolError
from mcubridge.protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS, Topic
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol.topics import topic_path

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.transport.mqtt")


class MqttTransport:
    """Simplified and robust MQTT transport (SIL-2)."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
    ) -> None:
        self.config = config
        self.state = state
        self.service: BridgeService | None = None

    def set_service(self, service: BridgeService) -> None:
        self.service = service

    async def run(self) -> None:
        """Main run loop with reconnection logic delegating to Tenacity."""
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
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            logger.critical("MQTT transport fatal error: %s", exc)
            raise
        except BaseExceptionGroup as eg:
            for exc in eg.exceptions:
                logger.critical("MQTT transport fatal error: %s", exc)
            raise

    async def _connect_session(self, tls_context: Any) -> None:
        connect_props = build_mqtt_connect_properties()

        # [SIL-2] Last Will and Testament
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
            logger.info("Connected to MQTT broker (Paho v2/MQTTv5).")

            # Subscribe
            topics = [
                (topic_path(self.state.mqtt_topic_prefix, t, *s), int(q))
                for t, s, q in MQTT_COMMAND_SUBSCRIPTIONS
            ]
            await client.subscribe(topics)
            logger.info("Subscribed to %d command topics.", len(topics))

            # Publish online status
            await client.publish(
                will_topic, b'{"status": "online"}', qos=1, retain=True
            )

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._publisher_loop(client))
                task_group.create_task(self._subscriber_loop(client))

    async def _publisher_loop(self, client: aiomqtt.Client) -> None:
        """Publishes messages with native retry and automatic spool fallback (SIL-2)."""
        try:
            while True:
                await self.flush_mqtt_spool()
                message = await self.state.mqtt_publish_queue.get()

                topic_name = message.topic_name
                props = message.to_paho_properties()
                payload = message.payload
                qos = int(message.qos)
                retain = message.retain

                @tenacity.retry(
                    wait=tenacity.wait_exponential(multiplier=0.1, max=10),
                    retry=tenacity.retry_if_exception_type(aiomqtt.MqttError),
                )
                async def _reliable_publish() -> None:
                    await client.publish(
                        topic_name, payload, qos=qos, retain=retain, properties=props
                    )

                published = False
                try:
                    await _reliable_publish()
                    self.state.mqtt_messages_published += 1
                    self.state.metrics.mqtt_messages_published.inc()
                    published = True
                except (aiomqtt.MqttError, OSError, RuntimeError) as exc:
                    logger.warning("Publish failure, moving to spool: %s", exc)
                    await self.stash_mqtt_message(message)
                finally:
                    if not published:
                        # Ensure we don't lose the message on unexpected errors
                        with contextlib.suppress(asyncio.QueueFull):
                            self.state.mqtt_publish_queue.put_nowait(message)
                    self.state.mqtt_publish_queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _subscriber_loop(self, client: aiomqtt.Client) -> None:
        try:
            async for message in client.messages:
                try:
                    topic_str = str(message.topic)
                    if topic_str and self.service:
                        await self.service.handle_mqtt_message(message)
                except Exception as e:
                    logger.error("Error processing message on %s: %s", message.topic, e)
        except asyncio.CancelledError:
            raise
        except aiomqtt.MqttError as exc:
            logger.warning("Subscriber loop error: %s", exc)
            raise

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Any | None = None,
    ) -> None:
        """Enqueues message with direct spool fallback if RAM queue is full (SIL-2)."""
        if reply_context:
            # Handle reply-to and correlation context
            props = getattr(reply_context, "properties", None)
            resp_topic = getattr(props, "ResponseTopic", None) if props else None
            corr_data = getattr(props, "CorrelationData", None) if props else None

            if resp_topic:
                message = msgspec.structs.replace(message, topic_name=resp_topic)
            if corr_data:
                message = msgspec.structs.replace(message, correlation_data=corr_data)

        # [SIL-2] Inject request context metadata for traceability
        user_props = list(message.user_properties)
        if reply_context:
            user_props.append(
                (
                    "bridge-request-topic",
                    str(getattr(reply_context, "topic", "unknown")),
                )
            )
        message = msgspec.structs.replace(message, user_properties=tuple(user_props))

        try:
            self.state.mqtt_publish_queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop oldest and stash it
            try:
                dropped = self.state.mqtt_publish_queue.get_nowait()
                self.state.mqtt_publish_queue.task_done()
                self.state.mqtt_dropped_messages += 1
                self.state.metrics.mqtt_messages_dropped.inc()
                await self.stash_mqtt_message(dropped)
                self.state.mqtt_publish_queue.put_nowait(message)
            except asyncio.QueueEmpty:
                self.state.mqtt_publish_queue.put_nowait(message)

    async def publish(self, topic: str, payload: bytes | str, **kwargs: Any) -> None:
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        message = QueuedPublish(
            topic_name=topic,
            payload=payload_bytes,
            qos=kwargs.get("qos", 0),
            retain=kwargs.get("retain", False),
            message_expiry_interval=kwargs.get("expiry"),
            user_properties=tuple(kwargs.get("properties", ())),
        )
        await self.enqueue_mqtt(message, reply_context=kwargs.get("reply_to"))

    def configure_spool(self, directory: str, limit: int) -> None:
        if self.state.mqtt_spool:
            self.state.mqtt_spool.close()
            self.state.mqtt_spool = None
        self.state.mqtt_spool_dir = directory
        self.state.mqtt_spool_limit = max(0, limit)

    async def initialize_spool(self) -> bool:
        """Single-point spooler initialization delegating RAM fallback to BridgeQueue."""
        if self.state.mqtt_spool_limit <= 0:
            return False
        try:
            self.state.mqtt_spool = await asyncio.to_thread(
                MQTTPublishSpool, self.state.mqtt_spool_dir, self.state.mqtt_spool_limit
            )
            self.state.mqtt_spool_degraded = self.state.mqtt_spool.is_degraded
            return True
        except (OSError, MQTTSpoolError) as exc:
            logger.error("Spooler initialization failed: %s", exc)
            self.state.mqtt_spool_degraded = True
            return False

    async def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        if not self.state.mqtt_spool:
            return False
        try:
            await asyncio.to_thread(self.state.mqtt_spool.append, message)
            self.state.mqtt_spooled_messages += 1
            self.state.metrics.mqtt_spooled_messages.inc()
            return True
        except Exception as exc:
            logger.warning("Stash failed: %s", exc)
            self.state.mqtt_spool_errors += 1
            return False

    async def flush_mqtt_spool(self) -> None:
        if not self.state.mqtt_spool or self.state.mqtt_publish_queue.full():
            return
        while not self.state.mqtt_publish_queue.full():
            msg = await asyncio.to_thread(self.state.mqtt_spool.pop_next)
            if not msg:
                break
            await self.state.mqtt_publish_queue.put(msg)
            self.state.mqtt_spooled_replayed += 1
