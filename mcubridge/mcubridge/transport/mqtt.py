"""MQTT transport implementation using aiomqtt."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING, cast

import aiomqtt
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt import build_mqtt_connect_properties
from mcubridge.protocol.topics import MQTT_COMMAND_SUBSCRIPTIONS, topic_path
from mcubridge.state.context import RuntimeState
from mcubridge.util import log_hexdump
from mcubridge.util.mqtt_helper import configure_tls_context
from transitions import Machine

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

logger = logging.getLogger("mcubridge")


class MqttTransport:
    """Handles MQTT connectivity and message queuing."""

    def __init__(self, state: RuntimeState, config: RuntimeConfig, service: BridgeService) -> None:
        self.state = state
        self.config = config
        self.service = service
        self._client: aiomqtt.Client | None = None
        self._connected_event = asyncio.Event()

        # FSM for connectivity state
        self._machine = Machine(
            model=self,
            states=["disconnected", "connecting", "connected"],
            initial="disconnected",
            ignore_invalid_triggers=True,
        )
        self._machine.add_transition("connect", "disconnected", "connecting")
        self._machine.add_transition("mark_connected", "connecting", "connected")
        self._machine.add_transition("mark_disconnected", "*", "disconnected")

    async def run(self) -> None:
        """Main MQTT loop with exponential backoff and spooling."""
        while True:
            try:
                self.trigger("connect")
                tls_context = configure_tls_context(self.config)
                connect_props = build_mqtt_connect_properties()

                async with aiomqtt.Client(
                    hostname=self.config.mqtt_host,
                    port=self.config.mqtt_port,
                    username=self.config.mqtt_user,
                    password=self.config.mqtt_pass,
                    protocol=aiomqtt.ProtocolVersion.V5,
                    tls_context=tls_context,
                    properties=connect_props,
                ) as client:
                    self._client = client
                    self.trigger("mark_connected")
                    self._connected_event.set()
                    logger.info("Connected to MQTT broker at %s:%d", self.config.mqtt_host, self.config.mqtt_port)

                    # Subscribe to command topics
                    subs = [(t, q) for t, q in MQTT_COMMAND_SUBSCRIPTIONS]
                    await client.subscribe(subs)
                    logger.info("Subscribed to %d command topics.", len(subs))

                    # Start workers
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._publisher_loop())
                        tg.create_task(self._receiver_loop())
                        tg.create_task(self._spool_flusher_loop())

            except (aiomqtt.MqttError, socket.gaierror, ConnectionError) as exc:
                logger.error("MQTT connection error: %s", exc)
            except Exception as exc:
                logger.exception("Unexpected error in MQTT link: %s", exc)
            finally:
                self.trigger("mark_disconnected")
                self._connected_event.clear()
                self._client = None
                await asyncio.sleep(5)

    def trigger(self, event: str) -> None:
        """Helper to safely trigger FSM transitions."""
        try:
            getattr(self, event)()
        except (AttributeError, Exception):
            pass

    async def _publisher_loop(self) -> None:
        """Process outgoing MQTT messages from the state queue."""
        while self._client:
            pub = await self.state.mqtt_publish_queue.get()
            try:
                published = False
                try:
                    await self._client.publish(
                        pub.topic,
                        pub.payload,
                        qos=pub.qos,
                        retain=pub.retain,
                        properties=pub.properties,
                    )
                    self.state.record_mqtt_publish(pub.topic)
                    published = True
                except aiomqtt.MqttError as exc:
                    logger.warning("MQTT publish failed: %s", exc)

                if not published:
                    self.state.stash_mqtt_message(pub.topic, pub.payload)

            except Exception as exc:
                logger.error("Error in MQTT publisher loop: %s", exc)
            finally:
                self.state.mqtt_publish_queue.task_done()

    async def _receiver_loop(self) -> None:
        """Process inbound MQTT messages from the broker."""
        if not self._client:
            return
        async for message in self._client.messages:
            topic = str(message.topic)
            payload = cast(bytes, message.payload)  # type: ignore[reportUnnecessaryCast]
            logger.debug("[MQTT -> DAEMON] %s (%d bytes)", topic, len(payload))
            log_hexdump(logger, logging.DEBUG, "[MQTT -> DAEMON]", payload)

            # Route to appropriate service
            route = topic_path(topic)
            if route:
                await self.service.handle_mqtt_message(route, message)

    async def _spool_flusher_loop(self) -> None:
        """Periodically attempt to flush spooled messages."""
        while self._client:
            await asyncio.sleep(30)
            if self._connected_event.is_set():
                count = await self.state.flush_mqtt_spool(self.enqueue_publish)
                if count > 0:
                    logger.info("Successfully flushed %d spooled messages.", count)

    async def enqueue_publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        """Enqueue a message for publication."""
        from mcubridge.protocol.structures import QueuedPublish
        await self.state.mqtt_publish_queue.put(
            QueuedPublish(topic=topic, payload=payload, qos=qos)
        )
