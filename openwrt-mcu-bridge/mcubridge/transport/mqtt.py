"""MQTT transport helpers for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiomqtt
import tenacity
from transitions import Machine

from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt import build_mqtt_connect_properties, build_mqtt_properties
from mcubridge.protocol import topic_path
from mcubridge.protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS
from mcubridge.state.context import RuntimeState
from mcubridge.util import log_hexdump
from mcubridge.util.mqtt_helper import configure_tls_context

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

logger = logging.getLogger("mcubridge")


def _log_retry_attempt(retry_state: tenacity.RetryCallState) -> None:
    if retry_state.attempt_number > 1:
        logger.info(
            "Reconnecting MQTT (attempt %d, next wait %.2fs)...",
            retry_state.attempt_number,
            retry_state.next_action.sleep if retry_state.next_action else 0,
        )


class MqttTransport:
    """MQTT transport with FSM-based state management."""

    # FSM States
    STATE_DISCONNECTED = "disconnected"
    STATE_CONNECTING = "connecting"
    STATE_SUBSCRIBING = "subscribing"
    STATE_READY = "ready"

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: BridgeService,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.fsm_state = self.STATE_DISCONNECTED

        self.machine = Machine(
            model=self,
            states=[
                self.STATE_DISCONNECTED,
                self.STATE_CONNECTING,
                self.STATE_SUBSCRIBING,
                self.STATE_READY,
            ],
            initial=self.STATE_DISCONNECTED,
            model_attribute="fsm_state",
            ignore_invalid_triggers=True,
        )

        self.machine.add_transition("connect", "*", self.STATE_CONNECTING)
        self.machine.add_transition("connected", self.STATE_CONNECTING, self.STATE_SUBSCRIBING)
        self.machine.add_transition("subscribed", self.STATE_SUBSCRIBING, self.STATE_READY)
        self.machine.add_transition("disconnect", "*", self.STATE_DISCONNECTED)

    async def run(self) -> None:
        """Main run loop with reconnection logic."""
        tls_context = configure_tls_context(self.config)
        reconnect_delay = max(1, self.config.reconnect_delay)

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60) + tenacity.wait_random(0, 2),
            retry=tenacity.retry_if_exception_type((aiomqtt.MqttError, OSError, asyncio.TimeoutError)),
            before_sleep=_log_retry_attempt,
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    try:
                        await self._connect_session(tls_context)
                    except* (aiomqtt.MqttError, OSError, asyncio.TimeoutError) as exc_group:
                        # Unwrap exception group to allow tenacity to retry
                        for exc in exc_group.exceptions:
                            logger.error("MQTT connection error: %s", exc)
                        if len(exc_group.exceptions) >= 1:
                            raise exc_group.exceptions[0]
                        raise
                    finally:
                        if self.fsm_state != self.STATE_DISCONNECTED:
                            self.trigger("disconnect")
        except asyncio.CancelledError:
            logger.info("MQTT transport stopping.")
            self.trigger("disconnect")
            raise

    async def _connect_session(self, tls_context: Any) -> None:
        connect_props = build_mqtt_connect_properties()

        # [SIL-2] Warn if connecting without authentication
        if not self.config.mqtt_user:
            logger.warning(
                "MQTT connecting without authentication (anonymous); "
                "consider setting mqtt_user/mqtt_pass for production"
            )

        self.trigger("connect")

        async with aiomqtt.Client(
            hostname=self.config.mqtt_host,
            port=self.config.mqtt_port,
            username=self.config.mqtt_user or None,
            password=self.config.mqtt_pass or None,
            tls_context=tls_context,
            logger=logging.getLogger("mcubridge.mqtt.client"),
            protocol=aiomqtt.ProtocolVersion.V5,
            clean_session=None,
            properties=connect_props,
        ) as client:
            self.trigger("connected")
            logger.info("Connected to MQTT broker (Paho v2/MQTTv5).")

            await self._subscribe_topics(client)
            self.trigger("subscribed")

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._publisher_loop(client))
                task_group.create_task(self._subscriber_loop(client))

    async def _subscribe_topics(self, client: aiomqtt.Client) -> None:
        topics: list[tuple[str, int]] = []
        for topic_enum, segments, qos in MQTT_COMMAND_SUBSCRIPTIONS:
            topics.append(
                (
                    topic_path(self.state.mqtt_topic_prefix, topic_enum, *segments),
                    int(qos),
                )
            )

        for topic, qos in topics:
            await client.subscribe(topic, qos=qos)

        logger.info("Subscribed to %d command topics.", len(topics))

    async def _publisher_loop(self, client: aiomqtt.Client) -> None:
        while True:
            # [OPTIMIZATION] Flush spool before processing new messages
            await self.state.flush_mqtt_spool()
            message = await self.state.mqtt_publish_queue.get()
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
                    self.state.mqtt_publish_queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning("MQTT queue full during shutdown; message dropped.")
                raise
            except aiomqtt.MqttError as exc:
                logger.warning("MQTT publish failed (%s); requeuing.", exc)
                try:
                    self.state.mqtt_publish_queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.error("MQTT spool full; message dropped.")
                raise
            finally:
                self.state.mqtt_publish_queue.task_done()

    async def _subscriber_loop(self, client: aiomqtt.Client) -> None:
        try:
            # [SIL-2] Use native aiomqtt filters for cleaner dispatching
            async for message in client.messages:
                if not str(message.topic):
                    continue
                if logger.isEnabledFor(logging.DEBUG):
                    payload_bytes = bytes(message.payload) if message.payload else b""
                    log_hexdump(logger, logging.DEBUG, f"MQTT SUB < {message.topic}", payload_bytes)

                try:
                    # Dispatch using native topic matching capability
                    await self.service.handle_mqtt_message(message)
                except (ValueError, TypeError, AttributeError, RuntimeError, KeyError) as e:
                    logger.exception("CRITICAL: Error processing MQTT topic %s: %s", message.topic, e)
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
    """Wrapper to run the MqttTransport."""
    transport = MqttTransport(config, state, service)
    await transport.run()
