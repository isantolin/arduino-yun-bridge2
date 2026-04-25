"""Console component for MCU/Linux interactions."""

from __future__ import annotations

import itertools
import structlog
from typing import TYPE_CHECKING

import msgspec
from aiomqtt.message import Message
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, ConsoleAction
from mcubridge.protocol.structures import ConsoleWritePacket, TopicRoute

from ..config.const import MQTT_EXPIRY_CONSOLE
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
    from ..transport.mqtt import MqttTransport
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
        mqtt_flow: MqttTransport,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.mqtt_flow = mqtt_flow

    async def handle_write(self, seq_id: int, packet: ConsoleWritePacket) -> None:
        """Handle CMD_CONSOLE_WRITE from MCU (remote console output)."""
        data = packet.data
        if not data:
            return

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.CONSOLE,
            ConsoleAction.OUT,
        )
        await self.mqtt_flow.publish(
            topic=topic,
            payload=data,
            expiry=MQTT_EXPIRY_CONSOLE,
        )

    async def handle_xoff(self, seq_id: int, _: bytes) -> None:
        logger.warning("MCU > XOFF received (seq=%d), pausing serial output.", seq_id)
        self.state.mcu_is_paused = True
        self.state.serial_tx_allowed.clear()

    async def handle_xon(self, seq_id: int, _: bytes) -> None:
        logger.info("MCU > XON received (seq=%d), resuming serial output.", seq_id)
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        await self.flush_queue()

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        payload = msgspec.convert(inbound.payload, bytes)
        await self._handle_mqtt_input(payload, inbound)
        return True

    async def _handle_mqtt_input(
        self,
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        # [SIL-2] Ensure we chunk data to fit into frames using Python's C core batched
        chunks = [
            bytes(c) for c in itertools.batched(payload, protocol.MAX_PAYLOAD_SIZE)
        ]
        if self.state.mcu_is_paused:
            logger.warning(
                "MCU paused, queueing %d console chunk(s) (%d bytes), hex=%s",
                len(chunks),
                len(payload),
                payload[:32].hex() if len(payload) > 32 else payload.hex(),
            )
            for chunk in chunks:
                if chunk:
                    self.state.console_to_mcu_queue.append(chunk)
            return

        for index, chunk in enumerate(chunks):
            if not chunk:
                continue

            # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
            frame_payload = msgspec.msgpack.encode(ConsoleWritePacket(data=chunk))

            send_ok = await self.serial_flow.send(
                Command.CMD_CONSOLE_WRITE.value,
                frame_payload,
            )
            if not send_ok:
                remaining = b"".join(chunks[index:])
                if remaining:
                    self.state.console_to_mcu_queue.append(remaining)
                logger.warning(
                    "Serial send failed for console input; payload queued for retry",
                )
                break

    async def flush_queue(self) -> None:
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buffered = self.state.console_to_mcu_queue.popleft()
            if not buffered:
                break

            chunks = [
                bytes(c) for c in itertools.batched(buffered, protocol.MAX_PAYLOAD_SIZE)
            ]
            for index, chunk in enumerate(chunks):
                if not chunk:
                    continue

                # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
                frame_payload = msgspec.msgpack.encode(ConsoleWritePacket(data=chunk))

                send_ok = await self.serial_flow.send(
                    Command.CMD_CONSOLE_WRITE.value,
                    frame_payload,
                )
                if send_ok:
                    continue
                unsent = b"".join(chunks[index:])
                if unsent:
                    self.state.console_to_mcu_queue.appendleft(unsent)
                logger.warning(
                    "Serial send failed while flushing console; chunk requeued",
                )
                return

    def on_serial_disconnected(self) -> None:
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()


__all__ = ["ConsoleComponent"]
