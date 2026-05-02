"""System component handling MCU system requests and MQTT interactions."""

from __future__ import annotations

import collections
import msgspec
import structlog
from typing import TYPE_CHECKING, Any

from aiomqtt.message import Message
from ..protocol.protocol import Command, SystemAction
from ..protocol.structures import (
    EnterBootloaderPacket,
    FreeMemoryResponsePacket,
    QueuedPublish,
    TopicRoute,
    VersionResponsePacket,
)

from ..config.const import MQTT_EXPIRY_DATASTORE, MQTT_EXPIRY_DEFAULT
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
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
        enqueue_mqtt: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.enqueue_mqtt = enqueue_mqtt
        self._pending_free_memory: collections.deque[Message] = collections.deque()
        self._pending_version: collections.deque[Message] = collections.deque()

    async def request_mcu_version(self, inbound: Message | None = None) -> bool:
        if len(self._pending_version) >= 10:
            return False

        if inbound is not None:
            self._pending_version.append(inbound)

        self.state.mcu_version = None
        return await self.serial_flow.send(Command.CMD_GET_VERSION.value, b"")

    async def handle_get_version_resp(self, _: int, payload: bytes) -> bool:
        try:
            packet = msgspec.msgpack.decode(payload, type=VersionResponsePacket)
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed VersionResponsePacket: %s", e)
            return False

        major, minor, patch = packet.major, packet.minor, packet.patch
        self.state.mcu_version = (major, minor, patch)
        logger.info("MCU Version confirmed: %d.%d.%d", major, minor, patch)

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.VERSION,
            SystemAction.VALUE,
        )
        reply_context = (
            self._pending_version.popleft() if self._pending_version else None
        )

        # Direct call to mqtt_flow.enqueue_mqtt
        payload_bytes = f"{major}.{minor}.{patch}".encode("utf-8")
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=payload_bytes,
                message_expiry_interval=MQTT_EXPIRY_DATASTORE,
            ),
            reply_context=reply_context,
        )
        return True

    async def handle_get_free_memory_resp(self, _: int, payload: bytes) -> bool:
        try:
            packet = msgspec.msgpack.decode(payload, type=FreeMemoryResponsePacket)
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed FreeMemoryResponsePacket: %s", e)
            return False

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.FREE_MEMORY,
            SystemAction.VALUE,
        )
        reply_context = (
            self._pending_free_memory.popleft() if self._pending_free_memory else None
        )
        # Direct call to mqtt_flow.enqueue_mqtt
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=str(packet.value).encode("utf-8"),
                message_expiry_interval=MQTT_EXPIRY_DEFAULT,
            ),
            reply_context=reply_context,
        )
        return True

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        action = route.identifier
        match action:
            case SystemAction.FREE_MEMORY:
                if len(self._pending_free_memory) < 10:
                    self._pending_free_memory.append(inbound)
                    return await self.serial_flow.send(
                        Command.CMD_GET_FREE_MEMORY.value, b""
                    )
                return True
            case SystemAction.VERSION:
                return await self.request_mcu_version(inbound)
            case SystemAction.BOOTLOADER:
                try:
                    # [SIL-2] Use converted bytes to match msgspec schema
                    payload = msgspec.convert(inbound.payload, bytes)
                    packet = msgspec.msgpack.decode(payload, type=EnterBootloaderPacket)
                    return await self.serial_flow.send(
                        Command.CMD_ENTER_BOOTLOADER.value,
                        msgspec.msgpack.encode(packet),
                    )
                except (ValueError, msgspec.MsgspecError) as e:
                    logger.warning("Malformed EnterBootloader request: %s", e)
                    return True
            case _:
                return False
        return False


__all__ = ["SystemComponent"]
