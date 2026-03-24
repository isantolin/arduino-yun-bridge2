"""System component handling MCU system requests and MQTT interactions."""

from __future__ import annotations

import collections
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from aiomqtt.message import Message
from mcubridge.protocol.protocol import Command, SystemAction
from mcubridge.protocol.structures import (
    EnterBootloaderPacket,
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
        send_ok = await self._safe_send_request(
            queue=self._pending_version,
            request=inbound,
            limit=10,
            command_id=Command.CMD_GET_VERSION.value,
            payload=b"",
        )
        if send_ok:
            self.state.mcu_version = None
        return send_ok

    async def handle_set_baudrate_resp(self, seq_id: int, payload: bytes) -> None:
        logger.info("MCU acknowledged baudrate change. Switching local UART...")
        # We need to signal the transport layer to change baudrate.
        # This is a bit of a layer violation or needs a callback.
        ack = getattr(self.ctx, "on_baudrate_change_ack", None)
        if ack is not None:
            await cast(Callable[[], Awaitable[None]], ack)()

    async def handle_get_free_memory_resp(self, seq_id: int, payload: bytes) -> None:
        packet = self._decode_payload(FreeMemoryResponsePacket, payload, Command.CMD_GET_FREE_MEMORY_RESP)
        if packet is None:
            return

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.FREE_MEMORY,
            SystemAction.VALUE,
        )
        reply_context = self._pending_free_memory.popleft() if self._pending_free_memory else None
        await self._publish_value(topic, str(packet.value), MQTT_EXPIRY_DEFAULT, reply_context)

    async def handle_get_version_resp(self, seq_id: int, payload: bytes) -> None:
        packet = self._decode_payload(VersionResponsePacket, payload, Command.CMD_GET_VERSION_RESP)
        if packet is None:
            return

        major, minor = packet.major, packet.minor
        self.state.mcu_version = (major, minor)
        reply_context = self._pending_version.popleft() if self._pending_version else None
        await self._publish_version((major, minor), reply_context)
        logger.info("MCU firmware version reported as %d.%d", major, minor)

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
        await self._publish_value(topic, f"{major}.{minor}", MQTT_EXPIRY_DATASTORE, reply_context)

    async def handle_mqtt(
        self,
        identifier: str,
        remainder: list[str],
        inbound: Message | None = None,
    ) -> bool:
        match identifier:
            case SystemAction.BOOTLOADER:
                packet = EnterBootloaderPacket(magic=0xDEADC0DE)
                logger.warning("MCU > Sending EnterBootloader command (DEADC0DE)")
                return await self.ctx.send_frame(Command.CMD_ENTER_BOOTLOADER.value, packet.encode())

            case SystemAction.FREE_MEMORY:
                if not (remainder and remainder[0] == SystemAction.GET):
                    return False
                return await self._safe_send_request(
                    queue=self._pending_free_memory,
                    request=inbound,
                    limit=10,
                    command_id=Command.CMD_GET_FREE_MEMORY.value,
                    payload=b"",
                )

            case SystemAction.VERSION:
                if not (remainder and remainder[0] == SystemAction.GET):
                    return False
                cached_version = self.state.mcu_version
                if cached_version is not None and inbound is not None:
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
