"""Pin management component for MCU and MQTT interactions."""

from __future__ import annotations

import logging
import struct

from aiomqtt.message import Message as MQTTMessage
from yunbridge.rpc.protocol import Command, Status
from yunbridge.rpc import protocol


from ...protocol.topics import Topic, topic_path
from ...mqtt.messages import QueuedPublish
from ...config.settings import RuntimeConfig
from ...state.context import PendingPinRequest, RuntimeState
from ...common import encode_status_reason
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

    async def handle_digital_read_resp(self, payload: bytes) -> None:
        if len(payload) != 1:
            logger.warning(
                "Malformed DIGITAL_READ_RESP payload: expected 1 byte, got %d",
                len(payload),
            )
            return

        value = payload[0]
        request: PendingPinRequest | None = None
        if self.state.pending_digital_reads:
            request = self.state.pending_digital_reads.popleft()
        else:
            logger.warning("Received DIGITAL_READ_RESP without pending request.")

        pin_value = request.pin if request else None
        topic = self._build_pin_topic(Topic.DIGITAL, pin_value)
        pin_label = str(pin_value) if pin_value is not None else "unknown"
        message = QueuedPublish(
            topic_name=topic,
            payload=str(value).encode("utf-8"),
            message_expiry_interval=5,
            user_properties=(("bridge-pin", pin_label),),
        )
        await self.ctx.enqueue_mqtt(
            message,
            reply_context=request.reply_context if request else None,
        )

    async def handle_analog_read_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed ANALOG_READ_RESP payload: expected 2 bytes, got %d",
                len(payload),
            )
            return

        value = int.from_bytes(payload, "big")
        request: PendingPinRequest | None = None
        if self.state.pending_analog_reads:
            request = self.state.pending_analog_reads.popleft()
        else:
            logger.warning("Received ANALOG_READ_RESP without pending request.")

        pin_value = request.pin if request else None
        topic = self._build_pin_topic(Topic.ANALOG, pin_value)
        pin_label = str(pin_value) if pin_value is not None else "unknown"
        message = QueuedPublish(
            topic_name=topic,
            payload=str(value).encode("utf-8"),
            message_expiry_interval=5,
            user_properties=(("bridge-pin", pin_label),),
        )
        await self.ctx.enqueue_mqtt(
            message,
            reply_context=request.reply_context if request else None,
        )

    async def handle_digital_read(self, payload: bytes) -> None:
        """Handle request from MCU to read a Linux GPIO."""
        # TODO: Implement actual GPIO reading via sysfs or libgpiod when available.
        # Currently, we maintain protocol symmetry but acknowledge missing HW support.
        logger.info("MCU requested DIGITAL_READ on Linux pin (unsupported).")
        await self.ctx.send_frame(
            Status.NOT_IMPLEMENTED.value, b"Linux GPIO read not available"
        )

    async def handle_analog_read(self, payload: bytes) -> None:
        """Handle request from MCU to read a Linux ADC channel."""
        logger.info("MCU requested ANALOG_READ on Linux pin (unsupported).")
        await self.ctx.send_frame(
            Status.NOT_IMPLEMENTED.value, b"Linux ADC read not available"
        )

    async def handle_mqtt(
        self,
        topic_type: str | Topic,
        parts: list[str],
        payload_str: str,
        inbound: MQTTMessage | None = None,
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

        if len(parts) == 4:
            subtopic = parts[3]
            if subtopic == "mode" and topic_enum == Topic.DIGITAL:
                await self._handle_mode_command(pin, pin_str, payload_str)
            elif subtopic == "read":
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
            struct.pack(protocol.PIN_WRITE_FORMAT, pin, mode),
        )

    async def _handle_read_command(
        self,
        topic_type: Topic,
        pin: int,
        inbound: MQTTMessage | None = None,
    ) -> None:
        command = (
            Command.CMD_DIGITAL_READ
            if topic_type == Topic.DIGITAL
            else Command.CMD_ANALOG_READ
        )
        queue_limit = self.state.pending_pin_request_limit
        queue = (
            self.state.pending_digital_reads
            if command == Command.CMD_DIGITAL_READ
            else self.state.pending_analog_reads
        )
        if len(queue) >= queue_limit:
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

        send_ok = await self.ctx.send_frame(
            command.value,
            struct.pack(protocol.PIN_READ_FORMAT, pin),
        )
        if send_ok:
            if command == Command.CMD_DIGITAL_READ:
                self.state.pending_digital_reads.append(
                    PendingPinRequest(pin=pin, reply_context=inbound)
                )
            else:
                self.state.pending_analog_reads.append(
                    PendingPinRequest(pin=pin, reply_context=inbound)
                )

    async def _handle_write_command(
        self, topic_type: Topic, pin: int, parts: list[str], payload_str: str
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
            if topic_type == Topic.DIGITAL
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
        inbound: MQTTMessage | None,
    ) -> None:
        topic = self._build_pin_topic(topic_type, pin)
        message = QueuedPublish(
            topic_name=topic,
            payload=b"",
            message_expiry_interval=5,
            user_properties=(
                ("bridge-pin", str(pin)),
                ("bridge-error", "pending-pin-overflow"),
            ),
        )
        await self.ctx.enqueue_mqtt(
            message,
            reply_context=inbound,
        )


__all__ = ["PinComponent"]
