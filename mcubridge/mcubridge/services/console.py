"""Console component for MCU/Linux interactions."""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

import msgspec
from aiomqtt.message import Message
from ..protocol.protocol import (
    Command,
    ConsoleAction,
)
from ..protocol.structures import (
    ConsoleWritePacket,
    QueuedPublish,
    TopicRoute,
)

from ..config.const import MQTT_EXPIRY_CONSOLE
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.console")


class ConsoleComponent:
    """Encapsulate remote console behaviour. [SIL-2]"""

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

    async def handle_write(self, seq_id: int, payload: bytes) -> None:
        """Handle CMD_CONSOLE_WRITE from MCU (remote console output)."""
        try:
            # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
            packet = msgspec.msgpack.decode(payload, type=ConsoleWritePacket)
            data = packet.data
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed console write from MCU: %s", e)
            return

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.CONSOLE,
            ConsoleAction.OUT,
        )
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=data,
                message_expiry_interval=MQTT_EXPIRY_CONSOLE,
            )
        )

    async def handle_xoff(self, seq_id: int, _: bytes) -> None:
        logger.warning("MCU > XOFF received (seq=%d), pausing serial output.", seq_id)
        self.state.mcu_is_paused = True
        self.state.serial_tx_allowed.clear()

    async def handle_xon(self, seq_id: int, _: bytes) -> None:
        logger.info("MCU > XON received (seq=%d), resuming serial output.", seq_id)
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Process inbound MQTT requests for console operations."""
        action = route.identifier
        match action:
            case ConsoleAction.IN:
                payload = msgspec.convert(inbound.payload, bytes)
                return await self._handle_mqtt_input(payload)
            case _:
                return False
        return False

    async def _handle_mqtt_input(self, payload: bytes) -> bool:
        """Process characters sent from MQTT to the MCU console."""
        if self.state.mcu_is_paused:
            self.state.console_to_mcu_queue.append(payload)
            return True

        # [SIL-2] Protocol: Wrap console input in a Packet for consistency.
        packet = ConsoleWritePacket(data=payload)
        return await self.serial_flow.send(
            Command.CMD_CONSOLE_WRITE.value, msgspec.msgpack.encode(packet)
        )

    async def flush_queue(self) -> None:
        """Attempt to send any buffered console output to the MCU."""
        # This implementation uses state.console_to_mcu_queue directly.
        # It's called by BridgeService upon reconnection.
        queue = self.state.console_to_mcu_queue
        while queue and not self.state.mcu_is_paused:
            chunk = queue.popleft()
            packet = ConsoleWritePacket(data=chunk)
            ok = await self.serial_flow.send(
                Command.CMD_CONSOLE_WRITE.value, msgspec.msgpack.encode(packet)
            )
            if not ok:
                queue.appendleft(chunk)
                logger.debug(
                    "Serial link saturated while flushing console; chunk requeued",
                )
                return

    async def on_serial_disconnected(self) -> None:
        """Force-resume MCU and clear buffers on disconnect. [SIL-2]"""
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        self.state.console_to_mcu_queue.clear()


__all__ = ["ConsoleComponent"]
