"""System component handling MCU system requests and MQTT interactions."""
from __future__ import annotations

import logging
from typing import Tuple

from yunbridge.rpc.protocol import Command

from ...const import TOPIC_SYSTEM
from ...mqtt import PublishableMessage
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
from .base import BridgeContext

logger = logging.getLogger("yunbridge.system")


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

    async def request_mcu_version(self) -> bool:
        send_ok = await self.ctx.send_frame(Command.CMD_GET_VERSION.value, b"")
        if send_ok:
            self.state.mcu_version = None
        return send_ok

    async def handle_get_free_memory_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed GET_FREE_MEMORY_RESP payload: %s", payload.hex()
            )
            return

        free_memory = int.from_bytes(payload, "big")
        topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_SYSTEM}/free_memory/value"
        )
        message = PublishableMessage(
            topic_name=topic,
            payload=str(free_memory).encode("utf-8"),
        )
        await self.ctx.enqueue_mqtt(message)

    async def handle_get_version_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed GET_VERSION_RESP payload: %s", payload.hex()
            )
            return

        major, minor = payload[0], payload[1]
        self.state.mcu_version = (major, minor)
        await self._publish_version((major, minor))
        logger.info("MCU firmware version reported as %d.%d", major, minor)

    async def handle_mqtt(
        self, identifier: str, remainder: list[str]
    ) -> bool:
        if identifier == "free_memory" and remainder and remainder[0] == "get":
            await self.ctx.send_frame(Command.CMD_GET_FREE_MEMORY.value, b"")
            return True

        if identifier == "version" and remainder and remainder[0] == "get":
            cached_version = self.state.mcu_version
            await self.request_mcu_version()
            if cached_version is not None:
                await self._publish_version(cached_version)
            return True

        return False

    async def _publish_version(self, version: Tuple[int, int]) -> None:
        major, minor = version
        topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_SYSTEM}/version/value"
        )
        message = PublishableMessage(
            topic_name=topic,
            payload=f"{major}.{minor}".encode("utf-8"),
        )
        await self.ctx.enqueue_mqtt(message)


__all__ = ["SystemComponent"]
