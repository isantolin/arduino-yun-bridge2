"""Console component for MCU/Linux interactions."""

from __future__ import annotations

import structlog

import msgspec
from aiomqtt.message import Message
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, ConsoleAction
from mcubridge.protocol.structures import ConsoleWritePacket, TopicRoute

from ..config.const import MQTT_EXPIRY_CONSOLE
from ..protocol.topics import Topic, topic_path
from ..util import chunk_bytes
from .base import BaseComponent

logger = structlog.get_logger("mcubridge.console")


class ConsoleComponent(BaseComponent):
    """Encapsulate remote console behaviour."""

    async def handle_write(self, seq_id: int, payload: bytes) -> None:
        """Handle CMD_CONSOLE_WRITE from MCU (remote console output)."""
        packet = self._decode_payload(
            ConsoleWritePacket,
            payload,
            Command.CMD_CONSOLE_WRITE,
        )
        if packet is None:
            return

        data = packet.data
        if not data:
            return

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.CONSOLE,
            ConsoleAction.OUT,
        )
        await self.ctx.publish(
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
        await self.handle_mqtt_input(payload, inbound)
        return True

    async def _handle_mqtt_input(
        self,
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        # [SIL-2] Ensure we chunk data to fit into frames
        chunks = chunk_bytes(payload, protocol.MAX_PAYLOAD_SIZE)
        if self.state.mcu_is_paused:
            logger.warning(
                "MCU paused, queueing %d console chunk(s) (%d bytes), hex=%s",
                len(chunks),
                len(payload),
                payload[:32].hex() if len(payload) > 32 else payload.hex(),
            )
            for chunk in chunks:
                if chunk:
                    self.state.enqueue_console_chunk(chunk)
            return

        for index, chunk in enumerate(chunks):
            if not chunk:
                continue

            # [SIL-2] Use structured packet encoding
            frame_payload = ConsoleWritePacket(data=chunk).encode()

            send_ok = await self.ctx.send_frame(
                Command.CMD_CONSOLE_WRITE.value,
                frame_payload,
            )
            if not send_ok:
                remaining = b"".join(chunks[index:])
                if remaining:
                    self.state.enqueue_console_chunk(remaining)
                logger.warning(
                    "Serial send failed for console input; payload queued for retry",
                )
                break

    async def flush_queue(self) -> None:
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buffered = self.state.pop_console_chunk()
            if not buffered:
                break

            chunks = chunk_bytes(buffered, protocol.MAX_PAYLOAD_SIZE)
            for index, chunk in enumerate(chunks):
                if not chunk:
                    continue

                # [SIL-2] Use structured packet encoding
                frame_payload = ConsoleWritePacket(data=chunk).encode()

                send_ok = await self.ctx.send_frame(
                    Command.CMD_CONSOLE_WRITE.value,
                    frame_payload,
                )
                if send_ok:
                    continue
                unsent = b"".join(chunks[index:])
                if unsent:
                    self.state.requeue_console_chunk_front(unsent)
                logger.warning(
                    "Serial send failed while flushing console; chunk requeued",
                )
                return

    def on_serial_disconnected(self) -> None:
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()


__all__ = ["ConsoleComponent"]
