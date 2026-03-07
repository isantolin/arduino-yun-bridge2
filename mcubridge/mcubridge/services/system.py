"""System component handling MCU system requests and MQTT interactions."""

from __future__ import annotations

import collections
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from aiomqtt.message import Message
from construct import ConstructError
from mcubridge.protocol.protocol import Command, SystemAction
from mcubridge.protocol.structures import (
    FreeMemoryResponsePacket,
    VersionResponsePacket,
)

from ..config.const import MQTT_EXPIRY_DATASTORE, MQTT_EXPIRY_DEFAULT
from ..config.settings import RuntimeConfig
from ..protocol.topics import Topic, topic_path
from ..state.context import RuntimeState
from .base import BaseComponent, BridgeContext

logger = logging.getLogger("mcubridge.system")


class SystemComponent(BaseComponent):
    """Encapsulate MCU system information flows."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        super().__init__(config, state, ctx)
        self._pending_free_memory: collections.deque[Message] = collections.deque()
        self._pending_version: collections.deque[Message] = collections.deque()

    async def request_mcu_version(self, inbound: Message | None = None) -> bool:
        # Use provided inbound message or a sentinel to track the request
        request = inbound if inbound is not None else cast(Message, object())
        return await self._safe_send_request(
            queue=self._pending_version,
            request=request,
            limit=10,
            command_id=Command.CMD_GET_VERSION.value,
            payload=b"",
        )

    async def handle_set_baudrate_resp(self, payload: bytes) -> None:
        logger.info("MCU acknowledged baudrate change. Switching local UART...")
        # We need to signal the transport layer to change baudrate.
        # This is a bit of a layer violation or needs a callback.
        ack = getattr(self.ctx, "on_baudrate_change_ack", None)
        if ack is not None:
            await cast(Callable[[], Awaitable[None]], ack)()

    async def handle_get_free_memory_resp(self, payload: bytes) -> None:
        try:
            packet = FreeMemoryResponsePacket.decode(payload, Command.CMD_GET_FREE_MEMORY_RESP)
        except (ConstructError, ValueError):
            logger.warning("Malformed GET_FREE_MEMORY_RESP payload: %s", payload.hex())
            return
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
        await self._publish_broadcast_and_reply(
            topic, payload_bytes, MQTT_EXPIRY_DEFAULT, reply_context,
        )

    async def handle_get_version_resp(self, payload: bytes) -> None:
        try:
            packet = VersionResponsePacket.decode(payload, Command.CMD_GET_VERSION_RESP)
            major, minor = packet.major, packet.minor
        except (ConstructError, ValueError):
            logger.warning("Malformed GET_VERSION_RESP payload: %s", payload.hex())
            return

        self.state.mcu_version = (major, minor)
        reply_context = self._pending_version.popleft() if self._pending_version else None
        await self._publish_version((major, minor), reply_context)
        logger.info("MCU firmware version reported as %d.%d", major, minor)

    async def handle_mqtt(
        self,
        identifier: str,
        remainder: list[str],
        inbound: Message | None = None,
    ) -> bool:
        if not (remainder and remainder[0] == SystemAction.GET):
            return False

        match identifier:
            case SystemAction.FREE_MEMORY:
                request = inbound if inbound is not None else cast(Message, object())
                return await self._safe_send_request(
                    queue=self._pending_free_memory,
                    request=request,
                    limit=10,
                    command_id=Command.CMD_GET_FREE_MEMORY.value,
                    payload=b"",
                )

            case SystemAction.VERSION:
                cached_version = self.state.mcu_version
                if cached_version is not None and inbound is not None:
                    await self._publish_version(cached_version, inbound)
                    return True

                return await self.request_mcu_version(inbound)

            case _:
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
        await self._publish_broadcast_and_reply(
            topic, payload_bytes, MQTT_EXPIRY_DATASTORE, reply_context,
        )

    async def _publish_broadcast_and_reply(
        self,
        topic: str,
        payload: bytes,
        expiry: int,
        reply_context: Message | None,
    ) -> None:
        """Publish broadcast to topic and, if present, a targeted reply."""
        if reply_context is not None:
            await self.ctx.publish(
                topic=topic,
                payload=payload,
                expiry=expiry,
                content_type="text/plain; charset=utf-8",
                reply_to=reply_context,
            )
        await self.ctx.publish(
            topic=topic,
            payload=payload,
            expiry=expiry,
            content_type="text/plain; charset=utf-8",
            reply_to=None,
        )


__all__ = ["SystemComponent"]
