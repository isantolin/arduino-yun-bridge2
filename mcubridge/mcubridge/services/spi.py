"""SPI service implementation for the MCU Bridge daemon."""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

import msgspec
from aiomqtt.message import Message

from ..protocol import structures
from ..protocol.protocol import Command
from ..protocol.structures import TopicRoute
from ..protocol.topics import Topic, topic_path
from .base import BaseComponent

if TYPE_CHECKING:
    from ..config.settings import RuntimeConfig
    from ..state.context import RuntimeState
    from .base import BridgeContext

logger = structlog.get_logger("mcubridge.service.spi")


class SpiComponent(BaseComponent):
    """Handles SPI bus operations."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState, ctx: BridgeContext):
        super().__init__(config, state, ctx)

    async def handle_mqtt_begin(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle SPI_BEGIN request from MQTT."""
        return await self.ctx.serial_flow.send(Command.CMD_SPI_BEGIN.value, b"")

    async def handle_mqtt_end(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle SPI_END request from MQTT."""
        return await self.ctx.serial_flow.send(Command.CMD_SPI_END.value, b"")

    async def handle_mqtt_config(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle SPI_CONFIG request from MQTT."""
        payload = msgspec.convert(inbound.payload, bytes)
        try:
            # Expecting JSON or MsgPack config
            # frequency, bit_order (0:LSB, 1:MSB), data_mode (0-3)
            data = msgspec.json.decode(payload)
            packet = structures.SpiConfigPacket(
                bit_order=int(data.get("bit_order", 1)),
                data_mode=int(data.get("data_mode", 0)),
                frequency=int(data.get("frequency", 4000000)),
            )
            return await self.ctx.serial_flow.send(
                Command.CMD_SPI_SET_CONFIG.value, packet.encode()
            )
        except (msgspec.DecodeError, ValueError, TypeError) as e:
            logger.warning("Malformed SPI config request: %s", e)
            return True

    async def handle_mqtt_transfer(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle SPI_TRANSFER request from MQTT."""
        payload = msgspec.convert(inbound.payload, bytes)
        packet = structures.SpiTransferPacket(data=payload)
        return await self.ctx.serial_flow.send(
            Command.CMD_SPI_TRANSFER.value, packet.encode()
        )

    async def handle_transfer_resp(
self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_SPI_TRANSFER_RESP from MCU."""
        try:
            packet = structures.SpiTransferResponsePacket.decode(payload)
            # Publish received bytes back to MQTT
            topic = topic_path(
                self.state.mqtt_topic_prefix, Topic.SPI, "transfer", "resp"
            )
            await self.ctx.mqtt_flow.publish(topic, packet.data)
            return True
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed SPI transfer response: %s", e)
            return False
