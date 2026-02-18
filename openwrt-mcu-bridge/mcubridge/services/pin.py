"""Pin management component for MCU and MQTT interactions."""

from __future__ import annotations

import collections
import logging
from typing import Callable

from aiomqtt.message import Message
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
from ..config.settings import RuntimeConfig
from ..protocol.encoding import encode_status_reason
from ..protocol.topics import Topic, topic_path
from ..state.context import PendingPinRequest, RuntimeState
from .base import BridgeContext

logger = logging.getLogger("mcubridge.pin")


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
        expected_size: int,
        resp_name: str,
        topic_type: Topic,
        parse_value: Callable[[bytes], int],
        pending_queue: collections.deque[PendingPinRequest],
    ) -> None:
        """Shared implementation for digital/analog read response handling."""
        if len(payload) != expected_size:
            logger.warning(
                "Malformed %s payload: expected %d byte(s), got %d",
                resp_name,
                expected_size,
                len(payload),
            )
            return

        value = parse_value(payload)
        request: PendingPinRequest | None = None
        if pending_queue:
            request = pending_queue.popleft()
        else:
            logger.warning("Received %s without pending request.", resp_name)

        pin_value = request.pin if request else None
        topic = self._build_pin_topic(topic_type, pin_value)
        pin_label = str(pin_value) if pin_value is not None else "unknown"

        await self.ctx.publish(
            topic=topic,
            payload=str(value).encode("utf-8"),
            expiry=MQTT_EXPIRY_PIN,
            properties=(("bridge-pin", pin_label),),
            reply_to=request.reply_context if request else None,
        )

    async def handle_digital_read_resp(self, payload: bytes) -> None:
        await self._handle_pin_read_resp(
            payload=payload,
            expected_size=1,
            resp_name="DIGITAL_READ_RESP",
            topic_type=Topic.DIGITAL,
            parse_value=self._parse_digital_read_value,
            pending_queue=self.state.pending_digital_reads,
        )

    async def handle_analog_read_resp(self, payload: bytes) -> None:
        await self._handle_pin_read_resp(
            payload=payload,
            expected_size=2,
            resp_name="ANALOG_READ_RESP",
            topic_type=Topic.ANALOG,
            parse_value=self._parse_analog_read_value,
            pending_queue=self.state.pending_analog_reads,
        )

    @staticmethod
    def _parse_digital_read_value(p: bytes) -> int:
        return DigitalReadResponsePacket.decode(p).value

    @staticmethod
    def _parse_analog_read_value(p: bytes) -> int:
        return AnalogReadResponsePacket.decode(p).value

    async def handle_mqtt(
        self,
        topic_type: str | Topic,
        parts: list[str],
        payload_str: str,
        inbound: Message | None = None,
    ) -> None:
        if len(parts) < 3:
            return

        if isinstance(topic_type, Topic):
            topic_enum = topic_type
        else:
            try:
                topic_enum = Topic(topic_type)
            except ValueError:
                return

        pin_str = parts[2]
        pin = self._parse_pin_identifier(pin_str)
        if pin < 0:
            return

        is_analog_read = len(parts) == 4 and parts[3] == PinAction.READ and topic_enum == Topic.ANALOG
        # Note: Analog write usually targets PWM pins which are subset of digital pins in Arduino numbering,
        # but capabilities struct reports 'num_analog_inputs' specifically for ADC.
        # We'll use digital limit for writes and analog limit for analog reads.

        if not self._validate_pin_access(pin, is_analog_read):
            return

        if len(parts) == 4:
            subtopic = parts[3]
            if subtopic == PinAction.MODE and topic_enum == Topic.DIGITAL:
                await self._handle_mode_command(pin, pin_str, payload_str)
            elif subtopic == PinAction.READ:
                await self._handle_read_command(topic_enum, pin, inbound)
            else:
                logger.debug("Unknown pin subtopic for %s: %s", pin_str, subtopic)
            return

        if len(parts) == 3:
            await self._handle_write_command(
                topic_enum,
                pin,
                parts,
                payload_str,
            )

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
        queue_limit = self.state.pending_pin_request_limit

        queue_len = (
            len(self.state.pending_digital_reads)
            if command == Command.CMD_DIGITAL_READ
            else len(self.state.pending_analog_reads)
        )
        if queue_len >= queue_limit:
            logger.warning(
                "Pending %s read queue saturated (limit=%d); " "dropping pin %s",
                topic_type,
                queue_limit,
                pin,
            )
            await self._notify_pin_queue_overflow(
                topic_type,
                pin,
                inbound,
            )
            return

        # Register pending request BEFORE sending frame to avoid race condition
        # where MCU response arrives before send_frame returns
        pending_request = PendingPinRequest(pin=pin, reply_context=inbound)
        if command == Command.CMD_DIGITAL_READ:
            self.state.pending_digital_reads.append(pending_request)
        else:
            self.state.pending_analog_reads.append(pending_request)

        # [SIL-2] Use structured packet encoding
        # PinReadPacket works for both Digital and Analog reads (1-byte pin payload)
        payload = PinReadPacket(pin=pin).encode()

        send_ok = await self.ctx.send_frame(command.value, payload)

        if not send_ok:
            # Remove pending request if send failed
            if command == Command.CMD_DIGITAL_READ:
                try:
                    self.state.pending_digital_reads.remove(pending_request)
                except ValueError:
                    pass  # Already consumed by response handler
            else:
                try:
                    self.state.pending_analog_reads.remove(pending_request)
                except ValueError:
                    pass  # Already consumed by response handler

    async def _handle_write_command(self, topic_type: Topic, pin: int, parts: list[str], payload_str: str) -> None:
        value = self._parse_pin_value(topic_type, payload_str)
        if value is None:
            logger.warning(
                "Invalid pin value topic=%s payload=%s",
                "/".join(parts),
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
        if pin_str.upper().startswith("A") and pin_str[1:].isdigit():
            return int(pin_str[1:])
        if pin_str.isdigit():
            return int(pin_str)
        return -1

    def _parse_pin_value(self, topic_type: Topic, payload_str: str) -> int | None:
        if not payload_str:
            return 0
        try:
            value = int(payload_str)
        except ValueError:
            return None

        if topic_type == Topic.DIGITAL and value in (0, 1):
            return value
        if topic_type == Topic.ANALOG and 0 <= value <= 255:
            return value
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
