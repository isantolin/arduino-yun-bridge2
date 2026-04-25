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

if TYPE_CHECKING:
    from .base import MqttFlow
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.service.spi")


class SpiComponent:
    """Handles SPI bus operations. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        mqtt_flow: MqttFlow,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.mqtt_flow = mqtt_flow

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Process inbound MQTT requests for SPI operations."""
        action = route.identifier
        payload = msgspec.convert(inbound.payload, bytes)
        try:
            match action:
                case "begin":
                    return await self.serial_flow.send(Command.CMD_SPI_BEGIN.value, b"")
                case "end":
                    return await self.serial_flow.send(Command.CMD_SPI_END.value, b"")
                case "config":
                    try:
                        # Expecting JSON or MsgPack config
                        # frequency, bit_order (0:LSB, 1:MSB), data_mode (0-3)
                        data = msgspec.json.decode(payload)
                        packet = structures.SpiConfigPacket(
                            bit_order=int(data.get("bit_order", 1)),
                            data_mode=int(data.get("data_mode", 0)),
                            frequency=int(data.get("frequency", 4000000)),
                        )
                        return await self.serial_flow.send(
                            Command.CMD_SPI_SET_CONFIG.value,
                            msgspec.msgpack.encode(packet),
                        )
                    except (msgspec.DecodeError, ValueError, TypeError) as e:
                        logger.warning("Malformed SPI config request: %s", e)
                        return False
                case "transfer":
                    # Simple case: raw bytes to transfer
                    packet = structures.SpiTransferPacket(data=payload)
                    return await self.serial_flow.send(
                        Command.CMD_SPI_TRANSFER.value, msgspec.msgpack.encode(packet)
                    )
                case _:
                    return False
        except (ValueError, TypeError, msgspec.ValidationError) as e:
            logger.error("Error handling SPI MQTT action %s: %s", action, e)
            return False

    async def handle_transfer_resp(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_SPI_TRANSFER_RESP from MCU."""
        try:
            packet = msgspec.msgpack.decode(
                payload, type=structures.SpiTransferResponsePacket
            )
            # Publish received bytes back to MQTT
            topic = topic_path(
                self.state.mqtt_topic_prefix, Topic.SPI, "transfer", "resp"
            )
            await self.mqtt_flow.publish(topic, packet.data)
            return True
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed SPI transfer response: %s", e)
            return False
