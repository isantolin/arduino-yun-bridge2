"""System component handling MCU system requests and MQTT interactions."""

from __future__ import annotations

import msgspec
import structlog
from typing import TYPE_CHECKING

from aiomqtt.message import Message
from ..protocol import protocol
from mcubridge.protocol.protocol import Command, SystemAction
from mcubridge.protocol.structures import (
    EnterBootloaderPacket,
    FreeMemoryResponsePacket,
    QueuedPublish,
    TopicRoute,
    VersionResponsePacket,
)

from ..config.const import MQTT_EXPIRY_DATASTORE, MQTT_EXPIRY_DEFAULT
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
    from ..transport.mqtt import MqttTransport
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.system")


class SystemComponent:
    """Encapsulate MCU system information flows. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        mqtt_flow: MqttTransport,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.mqtt_flow = mqtt_flow

    async def request_mcu_version(self, inbound: Message | None = None) -> bool:
        """Fetch version from MCU and sync internal state."""
        payload = await self.serial_flow.send_and_wait_payload(
            Command.CMD_GET_VERSION.value, b""
        )
        if payload is None:
            return False

        try:
            packet = msgspec.msgpack.decode(payload, type=VersionResponsePacket)
        except (ValueError, msgspec.MsgspecError):
            logger.warning("Malformed VersionResponsePacket payload: %s", payload.hex())
            return False

        version = (packet.major, packet.minor, packet.patch)
        self.state.mcu_version = version
        await self._publish_version(version, inbound)
        logger.info("MCU firmware version reported as %d.%d.%d", *version)
        return True

    async def _handle_free_memory_request(self, inbound: Message) -> bool:
        """Fetch free memory from MCU and publish result."""
        payload = await self.serial_flow.send_and_wait_payload(
            Command.CMD_GET_FREE_MEMORY.value, b""
        )
        if payload is None:
            return False

        try:
            packet = msgspec.msgpack.decode(payload, type=FreeMemoryResponsePacket)
        except (ValueError, msgspec.MsgspecError):
            logger.warning(
                "Malformed FreeMemoryResponsePacket payload: %s", payload.hex()
            )
            return False

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.FREE_MEMORY,
            SystemAction.VALUE,
        )

        # Broadcast update
        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=str(packet.value).encode("utf-8"),
                message_expiry_interval=MQTT_EXPIRY_DEFAULT,
            )
        )

        # Direct reply
        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=str(packet.value).encode("utf-8"),
                message_expiry_interval=MQTT_EXPIRY_DEFAULT,
            ),
            reply_context=inbound,
        )
        return True

    async def _publish_version(
        self,
        version: tuple[int, int, int],
        reply_context: Message | None = None,
    ) -> None:
        major, minor, patch = version
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.VERSION,
            SystemAction.VALUE,
        )
        # Direct call to mqtt_flow.enqueue_mqtt
        payload_bytes = f"{major}.{minor}.{patch}".encode("utf-8")
        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=payload_bytes,
                message_expiry_interval=MQTT_EXPIRY_DATASTORE,
            )
        )
        if reply_context is not None:
            await self.mqtt_flow.enqueue_mqtt(
                QueuedPublish(
                    topic_name=topic,
                    payload=payload_bytes,
                    message_expiry_interval=MQTT_EXPIRY_DATASTORE,
                ),
                reply_context=reply_context,
            )

    async def handle_mqtt(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        identifier = route.identifier
        remainder = list(route.remainder)
        match identifier:
            case SystemAction.BOOTLOADER:
                packet = EnterBootloaderPacket(magic=protocol.BOOTLOADER_MAGIC)
                logger.warning("MCU > Sending EnterBootloader command (DEADC0DE)")
                return await self.serial_flow.send(
                    Command.CMD_ENTER_BOOTLOADER.value, msgspec.msgpack.encode(packet)
                )

            case SystemAction.FREE_MEMORY:
                if not (remainder and remainder[0] == SystemAction.GET):
                    return False
                return await self._handle_free_memory_request(inbound)

            case SystemAction.VERSION:
                if not (remainder and remainder[0] == SystemAction.GET):
                    return False
                cached_version = self.state.mcu_version
                if cached_version is not None:
                    await self._publish_version(cached_version, inbound)

                # Always request fresh version to sync cache
                send_ok = await self.request_mcu_version(inbound)

                if cached_version is not None:
                    # Also broadcast current cached value
                    await self._publish_version(cached_version)

                return send_ok

            case _:
                return False


__all__ = ["SystemComponent"]
