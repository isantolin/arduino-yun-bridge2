"""Pin management component for MCU and MQTT interactions."""
from __future__ import annotations

import logging
import struct
from typing import Optional

from yunbridge.rpc.protocol import Command

from ...const import TOPIC_ANALOG, TOPIC_DIGITAL
from ...mqtt import PublishableMessage
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
from .base import BridgeContext

logger = logging.getLogger("yunbridge.pin")


class PinComponent:
    """Encapsulate pin read/write logic."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx

    async def handle_digital_read_resp(self, payload: bytes) -> None:
        if len(payload) != 1:
            logger.warning(
                "Malformed DIGITAL_READ_RESP payload: expected 1 byte, got %d",
                len(payload),
            )
            return

        value = payload[0]
        pin: Optional[int] = None
        if self.state.pending_digital_reads:
            pin = self.state.pending_digital_reads.popleft()
        else:
            logger.warning(
                "Received DIGITAL_READ_RESP without pending request."
            )

        topic = self._build_pin_topic(TOPIC_DIGITAL, pin)
        message = PublishableMessage(
            topic_name=topic,
            payload=str(value).encode("utf-8"),
        )
        await self.ctx.enqueue_mqtt(message)

    async def handle_analog_read_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed ANALOG_READ_RESP payload: expected 2 bytes, got %d",
                len(payload),
            )
            return

        value = int.from_bytes(payload, "big")
        pin: Optional[int] = None
        if self.state.pending_analog_reads:
            pin = self.state.pending_analog_reads.popleft()
        else:
            logger.warning(
                "Received ANALOG_READ_RESP without pending request."
            )

        topic = self._build_pin_topic(TOPIC_ANALOG, pin)
        message = PublishableMessage(
            topic_name=topic,
            payload=str(value).encode("utf-8"),
        )
        await self.ctx.enqueue_mqtt(message)

    async def handle_mqtt(
        self, topic_type: str, parts: list[str], payload_str: str
    ) -> None:
        if len(parts) < 3:
            return

        pin_str = parts[2]
        pin = self._parse_pin_identifier(pin_str)
        if pin < 0:
            return

        if len(parts) == 4:
            subtopic = parts[3]
            if subtopic == "mode" and topic_type == TOPIC_DIGITAL:
                await self._handle_mode_command(pin, pin_str, payload_str)
            elif subtopic == "read":
                await self._handle_read_command(topic_type, pin)
            else:
                logger.debug(
                    "Unknown pin subtopic for %s: %s", pin_str, subtopic
                )
            return

        if len(parts) == 3:
            await self._handle_write_command(
                topic_type,
                pin,
                parts,
                payload_str,
            )

    async def _handle_mode_command(
        self, pin: int, pin_str: str, payload_str: str
    ) -> None:
        try:
            mode = int(payload_str)
        except ValueError:
            logger.warning("Invalid mode payload for pin %s", pin_str)
            return

        if mode not in (0, 1, 2):
            logger.warning("Invalid digital mode %s", mode)
            return

        await self.ctx.send_frame(
            Command.CMD_SET_PIN_MODE.value,
            struct.pack(">BB", pin, mode),
        )

    async def _handle_read_command(self, topic_type: str, pin: int) -> None:
        command = (
            Command.CMD_DIGITAL_READ
            if topic_type == TOPIC_DIGITAL
            else Command.CMD_ANALOG_READ
        )
        send_ok = await self.ctx.send_frame(
            command.value,
            struct.pack(">B", pin),
        )
        if send_ok:
            if command == Command.CMD_DIGITAL_READ:
                self.state.pending_digital_reads.append(pin)
            else:
                self.state.pending_analog_reads.append(pin)

    async def _handle_write_command(
        self, topic_type: str, pin: int, parts: list[str], payload_str: str
    ) -> None:
        value = self._parse_pin_value(topic_type, payload_str)
        if value is None:
            logger.warning(
                "Invalid pin value topic=%s payload=%s",
                "/".join(parts),
                payload_str,
            )
            return

        command = (
            Command.CMD_DIGITAL_WRITE
            if topic_type == TOPIC_DIGITAL
            else Command.CMD_ANALOG_WRITE
        )
        await self.ctx.send_frame(
            command.value,
            struct.pack(">BB", pin, value),
        )

    def _parse_pin_identifier(self, pin_str: str) -> int:
        if pin_str.upper().startswith("A") and pin_str[1:].isdigit():
            return int(pin_str[1:])
        if pin_str.isdigit():
            return int(pin_str)
        return -1

    def _parse_pin_value(
        self, topic_type: str, payload_str: str
    ) -> Optional[int]:
        if not payload_str:
            return 0
        try:
            value = int(payload_str)
        except ValueError:
            return None

        if topic_type == TOPIC_DIGITAL and value in (0, 1):
            return value
        if topic_type == TOPIC_ANALOG and 0 <= value <= 255:
            return value
        return None

    def _build_pin_topic(self, topic_type: str, pin: Optional[int]) -> str:
        if pin is not None:
            return (
                f"{self.state.mqtt_topic_prefix}/{topic_type}/{pin}/value"
            )
        return f"{self.state.mqtt_topic_prefix}/{topic_type}/value"


__all__ = ["PinComponent"]
