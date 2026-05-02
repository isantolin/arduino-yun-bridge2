"""SPI service implementation for the MCU Bridge daemon."""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

import msgspec
from aiomqtt.message import Message

from ..protocol import structures
from ..protocol.protocol import Command
from ..protocol.structures import TopicRoute
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
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
        enqueue_mqtt: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.enqueue_mqtt = enqueue_mqtt

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
                    # [SIL-2] Use converted bytes to match msgspec schema
                    packet = msgspec.msgpack.decode(
                        payload, type=structures.SpiConfigPacket
                    )
                    return await self.serial_flow.send(
                        Command.CMD_SPI_SET_CONFIG.value,
                        msgspec.msgpack.encode(packet),
                    )
                case "transfer":
                    return await self.serial_flow.send(
                        Command.CMD_SPI_TRANSFER.value, payload
                    )
                case _:
                    return False
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed SPI request via MQTT: %s", e)
            return True
        return False

    async def handle_transfer_resp(self, _: int, payload: bytes) -> bool:
        """Process SPI transfer response from MCU and relay to MQTT."""
        try:
            packet = msgspec.msgpack.decode(payload, type=structures.SpiTransferPacket)

            # Log size for SIL-2 observability
            logger.debug(
                "SPI transfer complete: %d bytes relayed to MQTT", len(packet.data)
            )
            # Publish received bytes back to MQTT
            topic = topic_path(
                self.state.mqtt_topic_prefix, Topic.SPI, "transfer", "resp"
            )
            await self.enqueue_mqtt(
                structures.QueuedPublish(topic_name=topic, payload=packet.data)
            )
            return True
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed SPI transfer response: %s", e)
            return False


__all__ = ["SpiComponent"]
