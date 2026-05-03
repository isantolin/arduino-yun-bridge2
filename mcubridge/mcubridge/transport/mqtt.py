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
from typing import TYPE_CHECKING, Any, cast

import aiomqtt
import tenacity
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt import build_mqtt_connect_properties
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

        _retryable_excs = (aiomqtt.MqttError, OSError, asyncio.TimeoutError)

        def _is_retryable(e: BaseException) -> bool:
            if isinstance(e, _retryable_excs):
                return True
            if isinstance(e, BaseExceptionGroup):
                # [SIL-2] Iterate through group with proper type-aware inspection.
                return any(
                    _is_retryable(cast(BaseException, sub))
                    for sub in cast(Any, e).exceptions
                )
            return False

        def _retry_predicate(retry_state: tenacity.RetryCallState) -> bool:
            """[SIL-2] Check if the exception is retryable."""
            if not retry_state.outcome or not retry_state.outcome.failed:
                return False
            exc = retry_state.outcome.exception()
            return _is_retryable(exc) if exc else False

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60)
            + tenacity.wait_random(0, 2),
            retry=_retry_predicate,
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            after=lambda rs: self.state.metrics.retries.labels(
                component="mqtt_connect"
            ).inc(),
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
            # Flatten final fatal errors for logging
            for exc in eg.exceptions:
                logger.critical("MQTT transport fatal error: %s", exc)
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
                props = message.to_paho_properties()
                payload = message.payload
                qos = int(message.qos)
                retain = message.retain

                if logger.is_enabled_for(logging.DEBUG):
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
                    # [SIL-2] Direct metrics recording (No Wrapper)
                    self.state.mqtt_messages_published += 1
                    self.state.metrics.mqtt_messages_published.inc()
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

                if logger.is_enabled_for(logging.DEBUG):
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

            reply_correlation = (
                getattr(props, "CorrelationData", None) if props else None
            )
            if reply_correlation is not None:
                message_to_queue = msgspec.structs.replace(
                    message_to_queue, correlation_data=reply_correlation
                )

            origin_topic = str(reply_context.topic)
            new_props = message_to_queue.user_properties + (
                ("bridge-request-topic", origin_topic),
            )
            message_to_queue = msgspec.structs.replace(
                message_to_queue, user_properties=new_props
            )

        try:
            self.state.mqtt_publish_queue.put_nowait(message_to_queue)
        except (asyncio.QueueFull, asyncio.queues.QueueFull):
            # Dropping strategy: discard oldest, spool it, and insert new
            try:
                dropped = self.state.mqtt_publish_queue.get_nowait()
                self.state.mqtt_publish_queue.task_done()
                # [SIL-2] Direct metrics recording (No Wrapper)
                self.state.mqtt_drop_counts[dropped.topic_name] = (
                    self.state.mqtt_drop_counts.get(dropped.topic_name, 0) + 1
                )
                self.state.mqtt_dropped_messages += 1
                self.state.metrics.mqtt_messages_dropped.inc()

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
                self.state.mqtt_spool_failure_reason = (
                    spool_obj.last_error or "initialization_failed"
                )
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
            # NOTE: MQTTPublishSpool opens a diskcache.Cache (sqlite3 connection)
            # in the calling thread. All diskcache operations must stay in the
            # same thread (event loop) so that close() can reach the connection.
            # asyncio.to_thread is intentionally NOT used here.
            self.state.mqtt_spool = MQTTPublishSpool(
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
        if self.state.mqtt_spool is not None:
            with contextlib.suppress(OSError, AttributeError):
                self.state.mqtt_spool.close()
        self.state.mqtt_spool = None
        self.state.mqtt_spool_degraded = True
        self.state.mqtt_spool_failure_reason = reason
        if schedule_retry:
            self._schedule_spool_retry()

    def _schedule_spool_retry(self) -> None:
        """Calculate and set exponential backoff for spool retry."""
        self.state.mqtt_spool_retry_attempts = min(
            self.state.mqtt_spool_retry_attempts + 1, 6
        )
        delay = min(
            SPOOL_BACKOFF_MIN_SECONDS
            * (2 ** (self.state.mqtt_spool_retry_attempts - 1)),
            SPOOL_BACKOFF_MAX_SECONDS,
        )
        self.state.mqtt_spool_backoff_until = time.monotonic() + delay

    def _handle_mqtt_spool_failure(
        self, reason: str, exc: BaseException | None = None
    ) -> None:
        # [SIL-2] Direct metrics recording (No Wrapper)
        self.state.mqtt_spool_errors += 1
        self.state.metrics.mqtt_spool_errors.inc()
        if exc:
            self.state.mqtt_spool_last_error = str(exc)
        self._disable_mqtt_spool(reason)

    def _on_spool_fallback(self, reason: str, exc: BaseException | None = None) -> None:
        self.state.mqtt_spool_degraded = True
        self.state.mqtt_spool_failure_reason = reason
        if exc:
            self.state.mqtt_spool_last_error = str(exc)
        # [SIL-2] Direct metrics recording (No Wrapper)
        self.state.mqtt_spool_errors += 1
        self.state.metrics.mqtt_spool_errors.inc()

    async def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        if not await self.ensure_spool():
            return False
        spool = self.state.mqtt_spool
        if spool is None:
            return False
        try:
            # NOTE: diskcache append runs synchronously in the event loop thread
            # to keep all sqlite3 access in the same thread as close().
            spool.append(message)
            # [SIL-2] Direct metrics recording (No Wrapper)
            self.state.mqtt_spooled_messages += 1
            self.state.metrics.mqtt_spooled_messages.inc()
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
                # NOTE: diskcache ops run synchronously in the event loop thread
                # so that close() can later reach the same thread's sqlite3 conn.
                msg = spool.pop_next()
                if not msg:
                    break
                props = list(msg.user_properties) + [("bridge-spooled", "1")]
                final_msg = msgspec.structs.replace(msg, user_properties=props)
                try:
                    self.state.mqtt_publish_queue.put_nowait(final_msg)
                    self.state.mqtt_spooled_replayed += 1
                except asyncio.QueueFull:
                    spool.requeue(msg)
                    break
            except (MQTTSpoolError, OSError) as exc:
                self._handle_mqtt_spool_failure("pop_failed", exc=exc)
                break
