"""MQTT transport helpers for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import structlog

import msgspec
from aiomqtt.message import Message
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from mcubridge.protocol.structures import QueuedPublish
from typing import TYPE_CHECKING, Any, cast

import aiomqtt
import tenacity
from mcubridge.config.settings import RuntimeConfig
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
        self._client: aiomqtt.Client | None = None

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
                return any(
                    _is_retryable(cast(BaseException, sub))
                    for sub in cast(Any, e).exceptions
                )
            return False

        def _retry_predicate(retry_state: tenacity.RetryCallState) -> bool:
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
            for exc in eg.exceptions:
                logger.critical("MQTT transport fatal error: %s", exc)
            raise

    async def _connect_session(self, tls_context: Any) -> None:
        connect_props = Properties(PacketTypes.CONNECT)
        connect_props.SessionExpiryInterval = 0
        connect_props.RequestResponseInformation = 1
        connect_props.RequestProblemInformation = 1

        if not self.config.mqtt_user:
            logger.warning(
                "MQTT connecting without authentication (anonymous); "
                "consider setting mqtt_user/mqtt_pass for production"
            )

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

            topics = [
                (topic_path(self.state.mqtt_topic_prefix, t, *s), int(q))
                for t, s, q in MQTT_COMMAND_SUBSCRIPTIONS
            ]
            await client.subscribe(topics)
            logger.info("Subscribed to %d command topics.", len(topics))

            await client.publish(
                will_topic, b'{"status": "online"}', qos=1, retain=True
            )

            self._client = client
            try:
                await self._subscriber_loop(client)
            finally:
                self._client = None

    async def _subscriber_loop(self, client: aiomqtt.Client) -> None:
        try:
            async for message in client.messages:
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
        """Publishes directly using the active MQTT client."""
        if not self._client:
            self.state.mqtt_dropped_messages += 1
            self.state.metrics.mqtt_messages_dropped.inc()
            return

        message_to_send = message
        if reply_context is not None:
            props = reply_context.properties
            target_topic = (
                getattr(props, "ResponseTopic", None) if props else None
            ) or message.topic_name
            if target_topic != message_to_send.topic_name:
                message_to_send = msgspec.structs.replace(
                    message_to_send, topic_name=target_topic
                )

            reply_correlation = (
                getattr(props, "CorrelationData", None) if props else None
            )
            if reply_correlation is not None:
                message_to_send = msgspec.structs.replace(
                    message_to_send, correlation_data=reply_correlation
                )

            origin_topic = str(reply_context.topic)
            new_props = message_to_send.user_properties + (
                ("bridge-request-topic", origin_topic),
            )
            message_to_send = msgspec.structs.replace(
                message_to_send, user_properties=new_props
            )

        topic_name = message_to_send.topic_name
        props = message_to_send.to_paho_properties()
        payload = message_to_send.payload
        qos = int(message_to_send.qos)
        retain = message_to_send.retain

        if logger.is_enabled_for(logging.DEBUG):
            logger.log(
                logging.DEBUG,
                "[HEXDUMP] MQTT PUB > %s: %s",
                topic_name,
                payload.hex(" ").upper(),
            )

        try:
            await self._client.publish(
                topic_name,
                payload,
                qos=qos,
                retain=retain,
                properties=props,
            )
            self.state.metrics.mqtt_messages_published.inc()
        except (aiomqtt.MqttError, OSError, RuntimeError) as exc:
            logger.warning("MQTT direct publish failure: %s", exc)
            self.state.mqtt_drop_counts[topic_name] = (
                self.state.mqtt_drop_counts.get(topic_name, 0) + 1
            )
            self.state.mqtt_dropped_messages += 1
            self.state.metrics.mqtt_messages_dropped.inc()
