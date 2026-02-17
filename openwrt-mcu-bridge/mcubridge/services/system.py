"""System component handling MCU system requests and MQTT interactions."""

from __future__ import annotations

import collections
import logging
from typing import Awaitable, Callable, cast

from aiomqtt.message import Message
from construct import ConstructError
from mcubridge.protocol.protocol import Command, SystemAction
from mcubridge.protocol.structures import FreeMemoryResponsePacket, VersionResponsePacket

from ..config.const import MQTT_EXPIRY_DATASTORE, MQTT_EXPIRY_DEFAULT
from ..config.settings import RuntimeConfig
from ..protocol.topics import Topic, topic_path
from ..state.context import RuntimeState
from .base import BridgeContext

logger = logging.getLogger("mcubridge.system")


class SystemComponent:
    """Encapsulate MCU system information flows."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx
        self._pending_free_memory: collections.deque[Message] = collections.deque()
        self._pending_version: collections.deque[Message] = collections.deque()

    async def request_mcu_version(self) -> bool:
        send_ok = await self.ctx.send_frame(Command.CMD_GET_VERSION.value, b"")
        if send_ok:
            self.state.mcu_version = None
        return send_ok

    async def handle_set_baudrate_resp(self, payload: bytes) -> None:
        logger.info("MCU acknowledged baudrate change. Switching local UART...")
        # We need to signal the transport layer to change baudrate.
        # This is a bit of a layer violation or needs a callback.
        ack = getattr(self.ctx, "on_baudrate_change_ack", None)
        if ack is not None:
            await cast(Callable[[], Awaitable[None]], ack)()

    async def handle_get_free_memory_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning("Malformed GET_FREE_MEMORY_RESP payload: %s", payload.hex())
            return

        packet = FreeMemoryResponsePacket.decode(payload)
        free_memory = packet.value
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.FREE_MEMORY,
            SystemAction.VALUE,
        )
        reply_context = None
        if self._pending_free_memory:
            reply_context = self._pending_free_memory.popleft()

        payload_bytes = str(free_memory).encode("utf-8")
        if reply_context is not None:
            await self.ctx.publish(
                topic=topic,
                payload=payload_bytes,
                expiry=MQTT_EXPIRY_DEFAULT,
                content_type="text/plain; charset=utf-8",
                reply_to=reply_context,
            )
        await self.ctx.publish(
            topic=topic,
            payload=payload_bytes,
            expiry=MQTT_EXPIRY_DEFAULT,
            content_type="text/plain; charset=utf-8",
            reply_to=None,
        )

    async def handle_get_version_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning("Malformed GET_VERSION_RESP payload: %s", payload.hex())
            return

        try:
            packet = VersionResponsePacket.decode(payload)
            major, minor = packet.major, packet.minor
        except (ConstructError, ValueError):
            logger.warning("Malformed GET_VERSION_RESP payload: %s", payload.hex())
            return

        self.state.mcu_version = (major, minor)
        reply_context = None
        if self._pending_version:
            reply_context = self._pending_version.popleft()
        await self._publish_version((major, minor), reply_context)
        logger.info("MCU firmware version reported as %d.%d", major, minor)

    async def handle_mqtt(
        self,
        identifier: str,
        remainder: list[str],
        inbound: Message | None = None,
    ) -> bool:
        if identifier == SystemAction.FREE_MEMORY and remainder and remainder[0] == SystemAction.GET:
            if inbound is not None:
                self._pending_free_memory.append(inbound)
            await self.ctx.send_frame(Command.CMD_GET_FREE_MEMORY.value, b"")
            return True

        if identifier == SystemAction.VERSION and remainder and remainder[0] == SystemAction.GET:
            cached_version = self.state.mcu_version
            if cached_version is not None and inbound is not None:
                await self._publish_version(cached_version, inbound)
            else:
                if inbound is not None:
                    self._pending_version.append(inbound)
            await self.request_mcu_version()
            if cached_version is not None:
                await self._publish_version(cached_version)
            return True

        return False

    async def _publish_version(
        self,
        version: tuple[int, int],
        reply_context: Message | None = None,
    ) -> None:
        major, minor = version
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.VERSION,
            SystemAction.VALUE,
        )
        payload_bytes = f"{major}.{minor}".encode()
        if reply_context is not None:
            await self.ctx.publish(
                topic=topic,
                payload=payload_bytes,
                expiry=MQTT_EXPIRY_DATASTORE,
                content_type="text/plain; charset=utf-8",
                reply_to=reply_context,
            )
        await self.ctx.publish(
            topic=topic,
            payload=payload_bytes,
            expiry=MQTT_EXPIRY_DATASTORE,
            content_type="text/plain; charset=utf-8",
            reply_to=None,
        )


__all__ = ["SystemComponent"]
