"""Pin management component for MCU and MQTT interactions."""

from __future__ import annotations

import collections
import logging
from collections.abc import Callable
from typing import Any

from aiomqtt.message import Message
from construct import ConstructError
from mcubridge.protocol.protocol import Command, PinAction, Status
from mcubridge.protocol.structures import (
    AnalogReadResponsePacket,
    AnalogWritePacket,
    DigitalReadResponsePacket,
    DigitalWritePacket,
    PinModePacket,
    PinReadPacket,
)

from ..config.const import MQTT_EXPIRY_PIN
from ..protocol.encoding import encode_status_reason
from ..protocol.topics import Topic, topic_path
from ..state.context import PendingPinRequest
from .base import BaseComponent

logger = logging.getLogger("mcubridge.pin")


class PinComponent(BaseComponent):
    """Encapsulate pin read/write logic."""

    async def handle_unexpected_mcu_request(
        self,
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
        await self.ctx.send_frame(
            Status.NOT_IMPLEMENTED.value,
            encode_status_reason(reason),
        )
        return False

    async def _handle_pin_read_resp(
        self,
        *,
        payload: bytes,
        resp_name: str,
        topic_type: Topic,
        decode_packet: Callable[[bytes], Any],
        pending_queue: collections.deque[PendingPinRequest],
    ) -> None:
        """Shared implementation for digital/analog read response handling."""
        try:
            packet = decode_packet(payload)
            value = packet.value
            received_pin = packet.pin
        except Exception as exc:
            logger.error(
                "Malformed %s payload or decode error: %s (len=%d)",
                resp_name,
                exc,
                len(payload),
                exc_info=True,
            )
            return

        # [SIL-2] Find the matching request in the queue
        request: PendingPinRequest | None = None

        # Safe removal from deque
        try:
            for i in range(len(pending_queue)):
                if pending_queue[i].pin == received_pin:
                    request = pending_queue[i]
                    # Create a new deque without the found element to simulate remove()
                    # (Deques are small here, so this is acceptable for SIL-2 reliability)
                    new_queue = collections.deque(
                        [pending_queue[j] for j in range(len(pending_queue)) if j != i]
                    )
                    pending_queue.clear()
                    pending_queue.extend(new_queue)
                    break
        except (IndexError, AttributeError) as exc:
            logger.error("Error searching pending queue: %s", exc)

        if not request:
            logger.warning("Received %s for pin %d without matching pending request.", resp_name, received_pin)
            pin_value = received_pin
        else:
            pin_value = request.pin

        topic = self._build_pin_topic(topic_type, pin_value)
        pin_label = str(pin_value)

        await self.ctx.publish(
            topic=topic,
            payload=str(value).encode("utf-8"),
            expiry=MQTT_EXPIRY_PIN,
            properties=(("bridge-pin", pin_label),),
            reply_to=request.reply_context if request else None,
        )

    async def handle_digital_read_resp(self, payload: bytes) -> None:
        import sys
        await self._handle_pin_read_resp(
            payload=payload,
            resp_name="DIGITAL_READ_RESP",
            topic_type=Topic.DIGITAL,
            decode_packet=lambda p: DigitalReadResponsePacket.decode(p, Command.CMD_DIGITAL_READ_RESP),
            pending_queue=self.state.pending_digital_reads,
        )

    async def handle_analog_read_resp(self, payload: bytes) -> None:
        import sys
        await self._handle_pin_read_resp(
            payload=payload,
            resp_name="ANALOG_READ_RESP",
            topic_type=Topic.ANALOG,
            decode_packet=lambda p: AnalogReadResponsePacket.decode(p, Command.CMD_ANALOG_READ_RESP),
            pending_queue=self.state.pending_analog_reads,
        )


    async def handle_mqtt(
        self,
        topic_type: Topic,
        pin_str: str,
        action: str | None,
        payload_str: str,
        inbound: Message | None = None,
    ) -> bool:
        """Central entry point for pin-related MQTT messages."""
        pin = self._parse_pin_identifier(pin_str)
        if pin < 0:
            return False

        is_analog_read = action == PinAction.READ and topic_type == Topic.ANALOG
        if not self._validate_pin_access(pin, is_analog_read):
            return False

        if action == PinAction.MODE and topic_type == Topic.DIGITAL:
            await self._handle_mode_command(pin, pin_str, payload_str)
        elif action == PinAction.READ:
            await self._handle_read_command(topic_type, pin, inbound)
        elif action is None:
            await self._handle_write_command(topic_type, pin, payload_str)
        else:
            logger.debug("Unsupported pin action: %s", action)
            return False

        return True

    async def _handle_mode_command(self, pin: int, pin_str: str, payload_str: str) -> None:
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
        await self.ctx.send_frame(Command.CMD_SET_PIN_MODE.value, payload)

    async def _handle_read_command(
        self,
        topic_type: Topic,
        pin: int,
        inbound: Message | None = None,
    ) -> None:
        command = Command.CMD_DIGITAL_READ if topic_type == Topic.DIGITAL else Command.CMD_ANALOG_READ
        queue = (
            self.state.pending_digital_reads
            if command == Command.CMD_DIGITAL_READ
            else self.state.pending_analog_reads
        )

        pending_request = PendingPinRequest(pin=pin, reply_context=inbound)
        payload = PinReadPacket(pin=pin).encode()

        await self._safe_send_request(
            queue=queue,
            request=pending_request,
            limit=self.state.pending_pin_request_limit,
            command_id=command.value,
            payload=payload,
            on_overflow=lambda: self._notify_pin_queue_overflow(topic_type, pin, inbound),
        )
        print(f"!!! _handle_read_command FINISHED sending", flush=True)

    async def _handle_write_command(self, topic_type: Topic, pin: int, payload_str: str) -> None:
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

        await self.ctx.send_frame(command.value, payload)

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
            if (topic_type == Topic.DIGITAL and val in (0, 1)) or (topic_type == Topic.ANALOG and 0 <= val <= 255):
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
        await self.ctx.publish(
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
            logger.warning("Security Block: Pin %d exceeds hardware limit (%d).", pin, limit)
            return False
        return True


__all__ = ["PinComponent"]
