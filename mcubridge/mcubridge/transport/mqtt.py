"""MQTT transport helpers for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import aiomqtt
import tenacity
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt import build_mqtt_connect_properties, build_mqtt_properties
from mcubridge.protocol import topic_path
from mcubridge.protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS
from mcubridge.state.context import RuntimeState
from mcubridge.util import log_hexdump
from mcubridge.util.mqtt_helper import configure_tls_context
from transitions import Machine

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

logger = logging.getLogger("mcubridge")


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

    if TYPE_CHECKING:

        def trigger(self, event: str, *args: Any, **kwargs: Any) -> bool:
            """FSM trigger placeholder."""
            ...

    async def run(self) -> None:
        """Main run loop with reconnection logic."""
        if not self.config.mqtt_enabled:
            logger.info("MQTT transport is DISABLED in configuration.")
            return

        tls_context = configure_tls_context(self.config)
        reconnect_delay = max(1, self.config.reconnect_delay)

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60) + tenacity.wait_random(0, 2),
            retry=tenacity.retry_if_exception_type((aiomqtt.MqttError, OSError, asyncio.TimeoutError)),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
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
                    finally:
                        if self.fsm_state != self.STATE_DISCONNECTED:
                            self.trigger("disconnect")
        except asyncio.CancelledError:
            logger.info("MQTT transport stopping.")
            self.trigger("disconnect")
            raise

    async def _connect_session(self, tls_context: Any) -> None:
        connect_props = build_mqtt_connect_properties()

        # Create an on_log callback to hook into Paho's internal logging
        def on_log(client: Any, userdata: Any, level: int, buf: str) -> None:
            # Map Paho's log levels to Python's logging module
            # Paho uses: INFO=1, NOTICE=5, WARNING=4, ERR=8, DEBUG=16
            if level == 1:
                logger.debug("[PAHO] %s", buf)
            elif level == 5:
                logger.info("[PAHO] %s", buf)
            elif level == 4:
                logger.warning("[PAHO] %s", buf)
            elif level == 8:
                logger.error("[PAHO] %s", buf)
            elif level == 16:
                logger.debug("[PAHO] %s", buf)
            else:
                logger.debug("[PAHO %s] %s", level, buf)

        # [SIL-2] Warn if connecting without authentication
        if not self.config.mqtt_user:
            logger.warning(
                "MQTT connecting without authentication (anonymous); "
                "consider setting mqtt_user/mqtt_pass for production"
            )

        self.trigger("connect")

        client = aiomqtt.Client(
            hostname=self.config.mqtt_host,
            port=self.config.mqtt_port,
            username=self.config.mqtt_user or None,
            password=self.config.mqtt_pass or None,
            tls_context=tls_context,
            logger=logging.getLogger("mcubridge.mqtt.client"),
            protocol=aiomqtt.ProtocolVersion.V5,
            clean_session=None,
            properties=connect_props,
        )

        # [SIL-2] Safety: Wrap Paho's on_message to prevent internal exceptions (e.g. Invalid Topic)
        # from escaping to Paho's core, which causes log flooding in some environments.
        paho_client = getattr(client, "_client", None)
        if paho_client is None:
            logger.error("Could not access internal Paho client in aiomqtt. Callbacks not injected.")
            return

        original_on_message = paho_client.on_message

        def safe_on_message(c: Any, userdata: Any, msg: Any) -> None:
            if not msg or not msg.topic:
                return
            try:
                if original_on_message:
                    original_on_message(c, userdata, msg)
            except (OSError, RuntimeError, TypeError, ValueError) as e:
                # Silence the specific aiomqtt "Invalid topic" error to avoid log flood
                if "Invalid topic" not in str(e):
                    logger.error("Exception in MQTT on_message for topic %s: %s", msg.topic, e)

        # Inject callbacks directly into the underlying Paho client
        paho_client.on_log = on_log
        paho_client.on_message = safe_on_message

        async with client as connected_client:
            logger.info("Connected to MQTT broker (Paho v2/MQTTv5).")
            self.trigger("connected")
            self._mqtt_client = connected_client
            await self._subscribe_topics(client)
            self.trigger("subscribed")

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._publisher_loop(client))
                task_group.create_task(self._subscriber_loop(client))

    async def _subscribe_topics(self, client: aiomqtt.Client) -> None:
        topics = [(topic_path(self.state.mqtt_topic_prefix, t, *s), int(q)) for t, s, q in MQTT_COMMAND_SUBSCRIPTIONS]

        await client.subscribe(topics)

        logger.info("Subscribed to %d command topics.", len(topics))

    async def _publisher_loop(self, client: aiomqtt.Client) -> None:
        """Publishes messages from the internal queue to the MQTT broker."""

        @tenacity.retry(
            wait=tenacity.wait_exponential(multiplier=0.1, max=10),
            retry=tenacity.retry_if_exception_type(aiomqtt.MqttError),
            before_sleep=tenacity.before_sleep_log(logger, logging.DEBUG),
        )
        async def _reliable_publish(message: Any) -> None:
            """Internal helper for a single reliable publish attempt."""
            topic_name = message.topic_name
            props = build_mqtt_properties(message)

            if logger.isEnabledFor(logging.DEBUG):
                log_hexdump(logger, logging.DEBUG, f"MQTT PUB > {topic_name}", message.payload)

            await client.publish(
                topic_name,
                message.payload,
                qos=int(message.qos),
                retain=message.retain,
                properties=props,
            )

        try:
            while True:
                # [OPTIMIZATION] Flush spool before processing new messages
                await self.state.flush_mqtt_spool()

                # Wait for next message
                message = await self.state.mqtt_publish_queue.get()
                published = False
                try:
                    await _reliable_publish(message)
                    self.state.record_mqtt_publish()
                    published = True
                except aiomqtt.MqttError as exc:
                    logger.warning("MQTT persistent publish failure: %s", exc)
                    # Spool if tenacity stops or fatal
                    await self.state.stash_mqtt_message(message)
                except asyncio.CancelledError:
                    raise
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    logger.error("Unexpected error in MQTT publisher: %s", exc)
                    await self.state.stash_mqtt_message(message)
                finally:
                    if not published:
                        # [SIL-2] Fail-Safe: Re-enqueue if not sent (e.g. on cancellation)
                        try:
                            self.state.mqtt_publish_queue.put_nowait(message)
                        except asyncio.QueueFull:
                            await self.state.stash_mqtt_message(message)
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
                    log_hexdump(
                        logger,
                        logging.DEBUG,
                        f"MQTT SUB < {topic_str}",
                        payload_bytes,
                    )

                try:
                    # Dispatch using native topic matching capability
                    await self.service.handle_mqtt_message(message)
                except (AttributeError, IndexError, KeyError, OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.error("Error processing MQTT message on topic %s: %s", topic_str, e)
        except asyncio.CancelledError:
            with contextlib.suppress(asyncio.CancelledError):
                raise
        except aiomqtt.MqttError as exc:
            logger.warning("MQTT subscriber loop interrupted: %s", exc)
            raise
