"""Console component handling console bridging logic."""

from __future__ import annotations

import logging

from aiomqtt.message import Message
from mcubridge.protocol.protocol import Command, MAX_PAYLOAD_SIZE

from ..protocol.topics import Topic, topic_path
from ..mqtt.messages import QueuedPublish
from ..config.settings import RuntimeConfig
from ..state.context import RuntimeState
from .base import BridgeContext

logger = logging.getLogger("mcubridge.console")


class ConsoleComponent:
    """Encapsulate console handling for BridgeService."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx

    async def handle_write(self, payload: bytes) -> None:
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.CONSOLE,
            "out",
        )
        message = QueuedPublish(
            topic_name=topic,
            payload=payload,
            message_expiry_interval=5,
        )
        await self.ctx.enqueue_mqtt(message)

    async def handle_xoff(self, _: bytes) -> None:
        logger.warning("MCU > XOFF received, pausing serial output.")
        self.state.mcu_is_paused = True
        self.state.serial_tx_allowed.clear()

    async def handle_xon(self, _: bytes) -> None:
        logger.info("MCU > XON received, resuming serial output.")
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        await self.flush_queue()

    async def handle_mqtt_input(
        self,
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        chunks = self._iter_console_chunks(payload)
        if self.state.mcu_is_paused:
            logger.warning(
                "MCU paused, queueing %d console chunk(s) (%d bytes), hex=%s",
                len(chunks),
                len(payload),
                payload[:32].hex() if len(payload) > 32 else payload.hex(),
            )
            for chunk in chunks:
                if chunk:
                    self.state.enqueue_console_chunk(chunk, logger)
            return

        for index, chunk in enumerate(chunks):
            if not chunk:
                continue
            send_ok = await self.ctx.send_frame(
                Command.CMD_CONSOLE_WRITE.value,
                chunk,
            )
            if not send_ok:
                remaining = b"".join(chunks[index:])
                if remaining:
                    self.state.enqueue_console_chunk(remaining, logger)
                logger.warning(
                    "Serial send failed for console input; payload queued for " "retry",
                )
                break

    async def flush_queue(self) -> None:
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buffered = self.state.pop_console_chunk()
            chunks = self._iter_console_chunks(buffered)
            for index, chunk in enumerate(chunks):
                if not chunk:
                    continue
                send_ok = await self.ctx.send_frame(
                    Command.CMD_CONSOLE_WRITE.value,
                    chunk,
                )
                if send_ok:
                    continue
                unsent = b"".join(chunks[index:])
                if unsent:
                    self.state.requeue_console_chunk_front(unsent)
                logger.warning(
                    "Serial send failed while flushing console; chunk " "requeued",
                )
                return

    def on_serial_disconnected(self) -> None:
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()

    def _iter_console_chunks(self, payload: bytes) -> list[bytes]:
        if not payload:
            return []
        chunk_size = MAX_PAYLOAD_SIZE
        return [payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)]


__all__ = ["ConsoleComponent"]
