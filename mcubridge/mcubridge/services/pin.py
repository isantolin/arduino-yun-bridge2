"""Pin management component for MCU and MQTT interactions."""

from __future__ import annotations

import collections
import contextlib
import structlog
from typing import Any

import msgspec
from aiomqtt.message import Message
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, PinAction, Status
from mcubridge.protocol.structures import (
    AnalogReadResponsePacket,
    AnalogWritePacket,
    DigitalReadResponsePacket,
    DigitalWritePacket,
    PinModePacket,
    PinReadPacket,
    TopicRoute,
)

from ..config.const import MQTT_EXPIRY_PIN
from ..protocol.topics import Topic, topic_path
from ..state.context import PendingPinRequest
from .base import BaseComponent

logger = structlog.get_logger("mcubridge.pin")


class PinComponent(BaseComponent):
    """Encapsulate pin read/write logic."""

    async def handle_mcu_digital_read(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_DIGITAL_READ initiated by MCU."""
        return await self.handle_unexpected_mcu_request(
            seq_id, Command.CMD_DIGITAL_READ, payload
        )

    async def handle_mcu_analog_read(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_ANALOG_READ initiated by MCU."""
        return await self.handle_unexpected_mcu_request(
            seq_id, Command.CMD_ANALOG_READ, payload
        )

    async def handle_unexpected_mcu_request(
        self,
        seq_id: int,
        command: Command,
        payload: bytes,
    ) -> bool:
        """Reject MCU-initiated Linux pin operations that are unsupported."""

        origin = "pin-read-origin-mcu"
        if command == Command.CMD_DIGITAL_READ:
            detail = "linux_gpio_read_not_available"
        elif command == Command.CMD_ANALOG_READ:
            detail = "linux_adc_read_not_available"
        else:
            detail = "pin_request_not_supported"
        reason = f"{origin}:{detail}"

        logger.warning(
            "MCU requested unsupported pin command %s payload=%s",
            command.name,
            payload.hex(),
        )
        await self.ctx.serial_flow.send(
            Status.NOT_IMPLEMENTED.value,
            reason.encode("utf-8", errors="ignore")[: protocol.MAX_PAYLOAD_SIZE],
        )
        return False

    async def _handle_pin_read_resp(
        self,
        *,
        payload: bytes,
        resp_name: str,
        topic_type: Topic,
        packet_cls: Any,
        command_id: Command,
        pending_queue: collections.deque[PendingPinRequest],
    ) -> None:
        """Shared implementation for digital/analog read response handling."""
        try:
            packet = packet_cls.decode(payload, command_id)
        except ValueError:
            logger.warning("Malformed %s payload: %s", packet_cls.__name__, payload.hex())
            return

        value = packet.value
        request: PendingPinRequest | None = None
        if pending_queue:
            request = pending_queue.popleft()
        else:
            logger.warning("Received %s without pending request.", resp_name)

        pin_value = request.pin if request else None
        topic = self._build_pin_topic(topic_type, pin_value)
        pin_label = str(pin_value) if pin_value is not None else "unknown"

        # Special case: Pin reads require 'bridge-pin' property, so we use direct publish
        await self.ctx.mqtt_flow.publish(
            topic=topic,
            payload=str(value).encode("utf-8"),
            expiry=MQTT_EXPIRY_PIN,
            properties=(("bridge-pin", pin_label),),
            reply_to=request.reply_context if request else None,
        )

    async def handle_digital_read_resp(self, seq_id: int, payload: bytes) -> None:
        await self._handle_pin_read_resp(
            payload=payload,
            resp_name="DIGITAL_READ_RESP",
            topic_type=Topic.DIGITAL,
            packet_cls=DigitalReadResponsePacket,
            command_id=Command.CMD_DIGITAL_READ_RESP,
            pending_queue=self.state.pending_digital_reads,
        )

    async def handle_analog_read_resp(self, seq_id: int, payload: bytes) -> None:
        await self._handle_pin_read_resp(
            payload=payload,
            resp_name="ANALOG_READ_RESP",
            topic_type=Topic.ANALOG,
            packet_cls=AnalogReadResponsePacket,
            command_id=Command.CMD_ANALOG_READ_RESP,
            pending_queue=self.state.pending_analog_reads,
        )

    async def handle_mqtt(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        segments = list(route.segments)
        payload_bytes = msgspec.convert(inbound.payload, bytes)
        payload_str = payload_bytes.decode("utf-8", errors="ignore")
        if not segments:
            return True

        try:
            topic_enum = Topic(route.topic)
        except ValueError:
            return True

        pin_str = segments[0]
        pin = self._parse_pin_identifier(pin_str)
        if pin < 0:
            return True

        is_analog_read = (
            len(segments) == 2
            and segments[1] == PinAction.READ
            and topic_enum == Topic.ANALOG
        )

        if not self._validate_pin_access(pin, is_analog_read):
            return True

        if len(segments) == 2:
            subtopic = segments[1]
            if subtopic == PinAction.MODE and topic_enum == Topic.DIGITAL:
                await self._handle_mode_command(pin, pin_str, payload_str)
            elif subtopic == PinAction.READ:
                await self._handle_read_command(topic_enum, pin, inbound)
            else:
                logger.debug("Unknown pin subtopic for %s: %s", pin_str, subtopic)
            return True

        if len(segments) == 1:
            await self._handle_write_command(
                topic_enum,
                pin,
                payload_str,
            )
        return True

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

        # [SIL-2] Use structured packet encoding
        payload = PinModePacket(pin=pin, mode=mode).encode()
        await self.ctx.serial_flow.send(Command.CMD_SET_PIN_MODE.value, payload)

    async def _handle_read_command(
        self,
        topic_type: Topic,
        pin: int,
        inbound: Message | None = None,
    ) -> bool:
        command = (
            Command.CMD_DIGITAL_READ
            if topic_type == Topic.DIGITAL
            else Command.CMD_ANALOG_READ
        )
        queue = (
            self.state.pending_digital_reads
            if command == Command.CMD_DIGITAL_READ
            else self.state.pending_analog_reads
        )

        if len(queue) >= self.state.pending_pin_request_limit:
            await self._notify_pin_queue_overflow(topic_type, pin, inbound)
            return False

        pending_request = PendingPinRequest(pin=pin, reply_context=inbound)
        queue.append(pending_request)

        payload = PinReadPacket(pin=pin).encode()
        ok = await self.ctx.serial_flow.send(command.value, payload)
        if not ok:
            with contextlib.suppress(ValueError):
                queue.remove(pending_request)
        return ok

    async def _handle_write_command(
        self, topic_type: Topic, pin: int, payload_str: str
    ) -> None:
        value = self._parse_pin_value(topic_type, payload_str)
        if value is None:
            logger.warning(
                "Invalid pin value topic=%s payload=%s",
                topic_type.value,
                payload_str,
            )
            return

        if topic_type == Topic.DIGITAL:
            command = Command.CMD_DIGITAL_WRITE
            payload = DigitalWritePacket(pin=pin, value=value).encode()
        else:
            command = Command.CMD_ANALOG_WRITE
            payload = AnalogWritePacket(pin=pin, value=value).encode()

        await self.ctx.serial_flow.send(command.value, payload)

    def _parse_pin_identifier(self, pin_str: str) -> int:
        s = pin_str.upper()
        if s.startswith("A") and s[1:].isdigit():
            return int(s[1:])
        return int(pin_str) if pin_str.isdigit() else -1

    def _parse_pin_value(self, topic_type: Topic, payload_str: str) -> int | None:
        if not payload_str:
            return 0
        try:
            val = int(payload_str)
            if (topic_type == Topic.DIGITAL and val in (0, 1)) or (
                topic_type == Topic.ANALOG and 0 <= val <= 255
            ):
                return val
        except ValueError:
            pass
        return None

    def _build_pin_topic(self, topic_type: Topic, pin: int | None) -> str:
        segments: list[str] = []
        if pin is not None:
            segments.append(str(pin))
        segments.append("value")
        return topic_path(
            self.state.mqtt_topic_prefix,
            topic_type,
            *segments,
        )

    async def _notify_pin_queue_overflow(
        self,
        topic_type: Topic,
        pin: int,
        inbound: Message | None,
    ) -> None:
        topic = self._build_pin_topic(topic_type, pin)
        await self.ctx.mqtt_flow.publish(
            topic=topic,
            payload=b"",
            expiry=MQTT_EXPIRY_PIN,
            properties=(
                ("bridge-pin", str(pin)),
                ("bridge-error", "pending-pin-overflow"),
            ),
            reply_to=inbound,
        )

    def _validate_pin_access(self, pin: int, is_analog_input: bool) -> bool:
        caps = self.state.mcu_capabilities
        if caps is None:
            return True

        limit = caps.num_analog_inputs if is_analog_input else caps.num_digital_pins
        # Basic bounds check.
        # Note: Arduino pins are 0-indexed, so count=20 means 0..19.
        if pin >= limit:
            logger.warning(
                "Security Block: Pin %d exceeds hardware limit (%d).", pin, limit
            )
            return False
        return True


__all__ = ["PinComponent"]
