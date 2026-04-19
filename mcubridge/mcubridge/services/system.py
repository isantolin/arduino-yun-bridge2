"""System component handling MCU system requests and MQTT interactions."""

from __future__ import annotations

import collections
import contextlib
import structlog

from aiomqtt.message import Message
from ..protocol import protocol
from mcubridge.protocol.protocol import Command, SystemAction
from mcubridge.protocol.structures import (
    EnterBootloaderPacket,
    FreeMemoryResponsePacket,
    TopicRoute,
    VersionResponsePacket,
)

from ..config.const import MQTT_EXPIRY_DATASTORE, MQTT_EXPIRY_DEFAULT
from ..config.settings import RuntimeConfig
from ..protocol.topics import Topic, topic_path
from ..state.context import RuntimeState
from .base import BaseComponent, BridgeContext

logger = structlog.get_logger("mcubridge.system")


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
        if len(self._pending_version) >= 10:
            return False

        if inbound is not None:
            self._pending_version.append(inbound)

        ok = await self.ctx.serial_flow.send(Command.CMD_GET_VERSION.value, b"")
        if ok:
            self.state.mcu_version = None
        else:
            if inbound is not None:
                with contextlib.suppress(ValueError):
                    self._pending_version.remove(inbound)
        return ok

    async def handle_get_free_memory_resp(self, seq_id: int, payload: bytes) -> None:
        try:
            packet = FreeMemoryResponsePacket.decode(
                payload, Command.CMD_GET_FREE_MEMORY_RESP
            )
        except ValueError:
            logger.warning("Malformed FreeMemoryResponsePacket payload: %s", payload.hex())
            return

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.FREE_MEMORY,
            SystemAction.VALUE,
        )
        reply_context = (
            self._pending_free_memory.popleft() if self._pending_free_memory else None
        )
        # Direct call to RuntimeState.publish
        await self.ctx.mqtt_flow.publish(
            topic=topic,
            payload=str(packet.value),
            expiry=MQTT_EXPIRY_DEFAULT,
            reply_to=None,
        )
        if reply_context is not None:
            await self.ctx.mqtt_flow.publish(
                topic=topic,
                payload=str(packet.value),
                expiry=MQTT_EXPIRY_DEFAULT,
                reply_to=reply_context,
            )

    async def handle_get_version_resp(self, seq_id: int, payload: bytes) -> None:
        try:
            packet = VersionResponsePacket.decode(payload, Command.CMD_GET_VERSION_RESP)
        except ValueError:
            logger.warning("Malformed VersionResponsePacket payload: %s", payload.hex())
            return

        major, minor, patch = packet.major, packet.minor, packet.patch
        self.state.mcu_version = (major, minor, patch)
        reply_context = (
            self._pending_version.popleft() if self._pending_version else None
        )
        await self._publish_version((major, minor, patch), reply_context)
        logger.info("MCU firmware version reported as %d.%d.%d", major, minor, patch)

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
        # Direct call to RuntimeState.publish
        payload = f"{major}.{minor}.{patch}"
        await self.ctx.mqtt_flow.publish(
            topic=topic,
            payload=payload,
            expiry=MQTT_EXPIRY_DATASTORE,
            reply_to=None,
        )
        if reply_context is not None:
            await self.ctx.mqtt_flow.publish(
                topic=topic,
                payload=payload,
                expiry=MQTT_EXPIRY_DATASTORE,
                reply_to=reply_context,
            )

    async def handle_mqtt_bootloader(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle CMD_ENTER_BOOTLOADER request from MQTT."""
        packet = EnterBootloaderPacket(magic=protocol.BOOTLOADER_MAGIC)
        logger.warning("MCU > Sending EnterBootloader command (DEADC0DE)")
        return await self.ctx.serial_flow.send(
            Command.CMD_ENTER_BOOTLOADER.value, packet.encode()
        )

    async def handle_mqtt_free_memory(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle FREE_MEMORY request from MQTT."""
        remainder = list(route.remainder)
        if not (remainder and remainder[0] == SystemAction.GET):
            return False

        if len(self._pending_free_memory) >= 10:
            return False

        self._pending_free_memory.append(inbound)
        ok = await self.ctx.serial_flow.send(Command.CMD_GET_FREE_MEMORY.value, b"")
        if not ok:
            with contextlib.suppress(ValueError):
                self._pending_free_memory.append(inbound)
        return ok

    async def handle_mqtt_version(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle VERSION request from MQTT."""
        remainder = list(route.remainder)
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

    async def handle_mqtt_bridge(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle bridge status/snapshot requests from MQTT."""
        match list(route.remainder):
            case ["handshake", "get"]:
                # Access publish_bridge_snapshot via ctx if available, or just call it.
                # In dispatcher.py it was passed as a callback.
                # For SIL-2, we should have it as a method on MqttTransport or BridgeService.
                # Actually, BridgeService has _publish_bridge_snapshot.
                # Let's assume it's routed correctly for now or call the callback.
                return False # Fallback to dispatcher for complex orchestration

            case [("summary" | "state"), "get"]:
                return False # Fallback to dispatcher

            case _:
                return False

    async def handle_mqtt(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        identifier = route.identifier
        match identifier:
            case SystemAction.BOOTLOADER:
                return await self.handle_mqtt_bootloader(route, inbound)
            case SystemAction.FREE_MEMORY:
                return await self.handle_mqtt_free_memory(route, inbound)
            case SystemAction.VERSION:
                return await self.handle_mqtt_version(route, inbound)
            case _:
                return False


__all__ = ["SystemComponent"]
