"""MQTT transport helpers for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import structlog

import msgspec
import time
from aiomqtt.message import Message
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.mqtt.spool import MQTTPublishSpool, MQTTSpoolError
from mcubridge.config.const import SPOOL_BACKOFF_MIN_SECONDS, SPOOL_BACKOFF_MAX_SECONDS
from typing import TYPE_CHECKING, Any

import aiomqtt
import tenacity
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt import build_mqtt_connect_properties, build_mqtt_properties
from mcubridge.protocol.topics import topic_path
from mcubridge.protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS, Topic
from mcubridge.state.context import RuntimeState

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

logger = structlog.get_logger("mcubridge")


class MqttTransport:
    """Simplified MQTT transport (SIL-2)."""

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

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60)
            + tenacity.wait_random(0, 2),
            retry=tenacity.retry_if_exception_type(
                (aiomqtt.MqttError, OSError, asyncio.TimeoutError)
            ),
            before_sleep=_before_sleep,
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    try:
                        await self._connect_session(tls_context)
                    except* (
                        aiomqtt.MqttError,
                        OSError,
                        asyncio.TimeoutError,
                    ) as exc_group:
                        # Unwrap exception group to allow tenacity to retry
                        for exc in exc_group.exceptions:
                            logger.error("MQTT connection error: %s", exc)
                        if len(exc_group.exceptions) >= 1:
                            raise exc_group.exceptions[0]
                        raise
        except asyncio.CancelledError:
            logger.info("MQTT transport stopping.")
            raise

    async def _connect_session(self, tls_context: Any) -> None:
        connect_props = build_mqtt_connect_properties()

        # [SIL-2] Warn if connecting without authentication
        if not self.config.mqtt_user:
            logger.warning(
                "MQTT connecting without authentication (anonymous); "
                "consider setting mqtt_user/mqtt_pass for production"
            )

        # [SIL-2] Last Will and Testament: auto-publish offline status on unexpected disconnect
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

            # [SIL-2] Publish online status (retained) to complement the will message
            await client.publish(
                will_topic, b'{"status": "online"}', qos=1, retain=True
            )

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._publisher_loop(client))
                task_group.create_task(self._subscriber_loop(client))

    async def _publisher_loop(self, client: aiomqtt.Client) -> None:
        """Publishes messages from the internal queue to the MQTT broker."""

        try:
            while True:
                # [OPTIMIZATION] Flush spool before processing new messages
                await self.flush_mqtt_spool()

                # Wait for next message
                message = await self.state.mqtt_publish_queue.get()

                # [SIL-2] Pre-calculate properties ONCE before the retry block
                # to avoid redundant introspection logic.
                topic_name = message.topic_name
                props = build_mqtt_properties(message)
                payload = message.payload
                qos = int(message.qos)
                retain = message.retain

                if logger.isEnabledFor(logging.DEBUG):
                    logger.log(
                        logging.DEBUG,
                        "[HEXDUMP] MQTT PUB > %s: %s",
                        topic_name,
                        payload.hex(" ").upper(),
                    )

                @tenacity.retry(
                    wait=tenacity.wait_exponential(multiplier=0.1, max=10),
                    retry=tenacity.retry_if_exception_type(aiomqtt.MqttError),
                    before_sleep=tenacity.before_sleep_log(logger, logging.DEBUG),
                )
                async def _reliable_publish() -> None:
                    await client.publish(
                        topic_name,
                        payload,
                        qos=qos,
                        retain=retain,
                        properties=props,
                    )

                published = False
                should_requeue = False
                try:
                    await _reliable_publish()
                    self.state.record_mqtt_publish()
                    published = True
                except aiomqtt.MqttError as exc:
                    logger.warning("MQTT persistent publish failure: %s", exc)
                    should_requeue = not await self.stash_mqtt_message(message)
                except asyncio.CancelledError:
                    should_requeue = True
                    raise
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    logger.error("Unexpected error in MQTT publisher: %s", exc)
                    should_requeue = not await self.stash_mqtt_message(message)
                finally:
                    if not published and should_requeue:
                        # [SIL-2] Fail-Safe: Re-enqueue if not sent (e.g. on cancellation)
                        try:
                            self.state.mqtt_publish_queue.put_nowait(message)
                        except asyncio.QueueFull:
                            await self.stash_mqtt_message(message)
                    self.state.mqtt_publish_queue.task_done()

        except asyncio.CancelledError:
            logger.debug("MQTT publisher loop cancelled.")
            raise

    async def _subscriber_loop(self, client: aiomqtt.Client) -> None:
        try:
            # [SIL-2] Use native aiomqtt filters for cleaner dispatching
            async for message in client.messages:
                # Early validation of topic string to prevent Paho/aiomqtt edge cases
                try:
                    topic_str = str(message.topic)
                except (TypeError, ValueError):
                    continue

                if not topic_str:
                    continue

                if logger.isEnabledFor(logging.DEBUG):
                    payload_bytes = bytes(message.payload) if message.payload else b""
                    logger.log(
                        logging.DEBUG,
                        "MQTT SUB < %s: [%s]",
                        topic_str,
                        payload_bytes.hex(" ").upper() if payload_bytes else "",
                    )

                try:
                    # Dispatch using native topic matching capability
                    if self.service is not None:
                        await self.service.handle_mqtt_message(message)
                except (
                    AttributeError,
                    IndexError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as e:
                    logger.error(
                        "Error processing MQTT message on topic %s: %s", topic_str, e
                    )
                    payload_bytes = bytes(message.payload) if message.payload else b""
                    hexdump = payload_bytes.hex(" ").upper()
                    logger.error(
                        "[HEXDUMP] FAILED MQTT MSG < %s: %s", topic_str, hexdump
                    )
        except asyncio.CancelledError:
            with contextlib.suppress(asyncio.CancelledError):
                raise
        except aiomqtt.MqttError as exc:
            logger.warning("MQTT subscriber loop interrupted: %s", exc)
            raise

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        """Enqueues an MQTT message for publishing with an overflow dropping strategy."""
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
            self.state.mqtt_publish_queue.put_nowait(message_to_queue)
        except (asyncio.QueueFull, asyncio.queues.QueueFull):
            # Dropping strategy: discard oldest, spool it, and insert new
            try:
                dropped = self.state.mqtt_publish_queue.get_nowait()
                self.state.mqtt_publish_queue.task_done()
                self.state.record_mqtt_drop(dropped.topic_name)

                # Use background task for spooling to avoid blocking enqueue
                await self.stash_mqtt_message(dropped)

                # Now the queue definitely has room
                self.state.mqtt_publish_queue.put_nowait(message_to_queue)

                logger.warning(
                    "MQTT publish queue saturated; dropped oldest message from topic=%s",
                    dropped.topic_name,
                )
            except (asyncio.QueueEmpty, asyncio.queues.QueueEmpty):
                # Race condition: someone else emptied it? Just retry insertion
                self.state.mqtt_publish_queue.put_nowait(message_to_queue)

    async def publish(
        self,
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
        """Helper to enqueue an MQTT message without manually creating QueuedPublish."""
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
        await self.enqueue_mqtt(message, reply_context=reply_to)

    def configure_spool(self, directory: str, limit: int) -> None:
        if self.state.mqtt_spool:
            self.state.mqtt_spool.close()
            self.state.mqtt_spool = None
        self.state.mqtt_spool_dir = directory
        self.state.mqtt_spool_limit = max(0, limit)

    def initialize_spool(self) -> None:
        if not self.state.mqtt_spool_dir or self.state.mqtt_spool_limit <= 0:
            self._disable_mqtt_spool("disabled", schedule_retry=False)
            return
        try:
            if self.state.mqtt_spool:
                self.state.mqtt_spool.close()
                self.state.mqtt_spool = None
            spool_obj = MQTTPublishSpool(
                self.state.mqtt_spool_dir,
                self.state.mqtt_spool_limit,
                on_fallback=self._on_spool_fallback,
            )
            self.state.mqtt_spool = spool_obj
            if spool_obj.is_degraded:
                self.state.mqtt_spool_degraded = True
                self.state.mqtt_spool_failure_reason = spool_obj.last_error or "initialization_failed"
                self.state.mqtt_spool_last_error = spool_obj.last_error
            else:
                self.state.mqtt_spool_degraded = False
                self.state.mqtt_spool_failure_reason = None
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("initialization_failed", exc=exc)

    async def ensure_spool(self) -> bool:
        if self.state.mqtt_spool:
            return True
        if (
            not self.state.mqtt_spool_dir
            or self.state.mqtt_spool_limit <= 0
            or self._spool_backoff_remaining() > 0
        ):
            return False
        try:
            self.state.mqtt_spool = await asyncio.to_thread(
                MQTTPublishSpool,
                self.state.mqtt_spool_dir,
                self.state.mqtt_spool_limit,
                on_fallback=self._on_spool_fallback,
            )
            if self.state.mqtt_spool.is_degraded:
                self.state.mqtt_spool_degraded = True
                self.state.mqtt_spool_failure_reason = (
                    self.state.mqtt_spool.last_error or "reactivation_failed"
                )
                self.state.mqtt_spool_last_error = self.state.mqtt_spool.last_error
            else:
                self.state.mqtt_spool_degraded = False
                self.state.mqtt_spool_failure_reason = None
            self.state.mqtt_spool_recoveries += 1
            return True
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("reactivation_failed", exc=exc)
            return False

    def _spool_backoff_remaining(self) -> float:
        return (
            max(0.0, self.state.mqtt_spool_backoff_until - time.monotonic())
            if self.state.mqtt_spool_backoff_until > 0
            else 0.0
        )

    def _disable_mqtt_spool(self, reason: str, schedule_retry: bool = True) -> None:
        if self.state.mqtt_spool:
            with contextlib.suppress(OSError, AttributeError):
                self.state.mqtt_spool.close()
        self.state.mqtt_spool = None
        self.state.mqtt_spool_degraded = True
        self.state.mqtt_spool_failure_reason = reason
        if schedule_retry:
            self._schedule_spool_retry()

    def _schedule_spool_retry(self) -> None:
        """Calculate and set exponential backoff for spool retry."""
        self.state.mqtt_spool_retry_attempts = min(self.state.mqtt_spool_retry_attempts + 1, 6)
        delay = min(
            SPOOL_BACKOFF_MIN_SECONDS * (2 ** (self.state.mqtt_spool_retry_attempts - 1)),
            SPOOL_BACKOFF_MAX_SECONDS,
        )
        self.state.mqtt_spool_backoff_until = time.monotonic() + delay

    def _handle_mqtt_spool_failure(
        self, reason: str, exc: BaseException | None = None
    ) -> None:
        self.state.record_mqtt_spool_error()
        if exc:
            self.state.mqtt_spool_last_error = str(exc)
        self._disable_mqtt_spool(reason)

    def _on_spool_fallback(self, reason: str, exc: BaseException | None = None) -> None:
        self.state.mqtt_spool_degraded = True
        self.state.mqtt_spool_failure_reason = reason
        if exc:
            self.state.mqtt_spool_last_error = str(exc)
        self.state.record_mqtt_spool_error()

    async def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        if not await self.ensure_spool():
            return False
        spool = self.state.mqtt_spool
        if spool is None:
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, spool.append, message)
            self.state.record_mqtt_spool()
            return True
        except (MQTTSpoolError, OSError) as exc:
            self._handle_mqtt_spool_failure("append_failed", exc=exc)
            return False

    async def flush_mqtt_spool(self) -> None:
        if not await self.ensure_spool():
            return
        spool = self.state.mqtt_spool
        if spool is None:
            return
        while self.state.mqtt_publish_queue.qsize() < self.state.mqtt_queue_limit:
            try:
                msg = await asyncio.to_thread(spool.pop_next)
                if not msg:
                    break
                props = list(msg.user_properties) + [("bridge-spooled", "1")]
                final_msg = msgspec.structs.replace(msg, user_properties=props)
                try:
                    self.state.mqtt_publish_queue.put_nowait(final_msg)
                    self.state.mqtt_spooled_replayed += 1
                except asyncio.QueueFull:
                    # Re-spool if queue became full between qsize check and put
                    await asyncio.to_thread(spool.requeue, msg)
                    break
            except (MQTTSpoolError, OSError) as exc:
                self._handle_mqtt_spool_failure("pop_failed", exc=exc)
                break

