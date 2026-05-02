"""Pin management component for MCU and MQTT interactions."""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

import msgspec
from aiomqtt.message import Message
from ..protocol.protocol import Command
from ..protocol.structures import (
    AnalogReadResponsePacket,
    AnalogWritePacket,
    DigitalReadResponsePacket,
    DigitalWritePacket,
    PinModePacket,
    PinReadPacket,
    QueuedPublish,
    TopicRoute,
)

from ..config.const import MQTT_EXPIRY_PIN
from ..protocol.topics import Topic, topic_path
from ..state.context import PendingPinRequest

if TYPE_CHECKING:
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.pin")


class PinComponent:
    """Encapsulate pin read/write logic. [SIL-2]"""

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

    async def handle_digital_read_resp(self, seq_id: int, payload: bytes) -> bool:
        """Process digital read response from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=DigitalReadResponsePacket)
            await self._process_pin_response(Topic.DIGITAL, packet.value)
            return True
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed digital read response: %s", e)
            return False

    async def handle_analog_read_resp(self, seq_id: int, payload: bytes) -> bool:
        """Process analog read response from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=AnalogReadResponsePacket)
            await self._process_pin_response(Topic.ANALOG, packet.value)
            return True
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed analog read response: %s", e)
            return False

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Process inbound MQTT requests for pin operations."""
        # Identification
        try:
            pin = int(route.identifier)
        except ValueError:
            return False

        if not self._validate_pin_limit(pin):
            return True

        action = route.remainder[0] if route.remainder else "get"
        payload = msgspec.convert(inbound.payload, bytes)

        match action:
            case "mode":
                # [SIL-2] Pin mode validation must happen on both sides.
                # Valid modes: 0=INPUT, 1=OUTPUT, 2=INPUT_PULLUP
                try:
                    mode = int(payload.decode())
                    if mode not in (0, 1, 2):
                        raise ValueError("Invalid mode")
                    packet = PinModePacket(pin=pin, mode=mode)
                    return await self.serial_flow.send(
                        Command.CMD_SET_PIN_MODE.value, msgspec.msgpack.encode(packet)
                    )
                except (ValueError, UnicodeDecodeError) as e:
                    logger.warning("Invalid pin mode request for pin %d: %s", pin, e)
                    return True

            case str() if action in ("get", "read"):
                # Digital or Analog depending on topic
                cmd = (
                    Command.CMD_DIGITAL_READ
                    if route.topic == Topic.DIGITAL
                    else Command.CMD_ANALOG_READ
                )
                packet = PinReadPacket(pin=pin)

                # Store request for matching when response arrives
                queue = (
                    self.state.pending_digital_reads
                    if route.topic == Topic.DIGITAL
                    else self.state.pending_analog_reads
                )

                if len(queue) >= self.state.pending_pin_request_limit:
                    queue.popleft()

                queue.append(PendingPinRequest(pin=pin, reply_context=inbound))

                return await self.serial_flow.send(
                    cmd.value, msgspec.msgpack.encode(packet)
                )

            case str() if action in ("write", "0", "1", "on", "off") or action.isdigit():
                # Write operation (Digital)
                if route.topic != Topic.DIGITAL:
                    return False
                
                try:
                    val_str = action if (action.isdigit() or action in ("0", "1", "on", "off")) else inbound.payload.decode()
                    val = 1 if val_str in ("1", "on") else 0
                    packet = DigitalWritePacket(pin=pin, value=val)
                    return await self.serial_flow.send(
                        Command.CMD_DIGITAL_WRITE.value, msgspec.msgpack.encode(packet)
                    )
                except (ValueError, UnicodeDecodeError):
                    return False

            case str() if route.topic == Topic.ANALOG:
                # Write operation (Analog/PWM)
                try:
                    val_str = action if action.isdigit() else inbound.payload.decode()
                    val = int(val_str)
                    packet = AnalogWritePacket(pin=pin, value=val)
                    return await self.serial_flow.send(
                        Command.CMD_ANALOG_WRITE.value, msgspec.msgpack.encode(packet)
                    )
                except (ValueError, UnicodeDecodeError):
                    return False
            case _:
                return False

        return False

    async def handle_unexpected_mcu_request(
        self, seq_id: int, cmd: Command, payload: bytes
    ) -> bool:
        """Handle pin requests initiated by the MCU (unsupported by protocol design)."""
        logger.warning(
            "MCU initiated unexpected pin request %s (seq=%d); ignoring.",
            cmd.name,
            seq_id,
        )
        return False

    async def _process_pin_response(self, topic_type: Topic, value: int) -> None:
        queue = (
            self.state.pending_digital_reads
            if topic_type == Topic.DIGITAL
            else self.state.pending_analog_reads
        )

        request = queue.popleft() if queue else None
        pin_value = request.pin if request else None
        topic = self._build_pin_topic(topic_type, pin_value)
        pin_label = str(pin_value) if pin_value is not None else "unknown"
        reply_context = request.reply_context if request else None

        # Special case: Pin reads require 'bridge-pin' property, so we use direct enqueue_mqtt
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=str(value).encode("utf-8"),
                message_expiry_interval=MQTT_EXPIRY_PIN,
                user_properties=(("bridge-pin", pin_label),),
            ),
            reply_context=reply_context,
        )

    def _build_pin_topic(self, topic_type: Topic, pin: int | None) -> str:
        segments = ["value"]
        if pin is not None:
            segments.append(str(pin))
        return topic_path(self.state.mqtt_topic_prefix, topic_type, *segments)

    def _validate_pin_limit(self, pin: int) -> bool:
        """Validate pin number against hardware traits."""
        # [SIL-2] Fail-safe: digital pins 0..19.
        limit = 20
        if pin >= limit:
            logger.warning(
                "Security Block: Pin %d exceeds hardware limit (%d).", pin, limit
            )
            return False
        return True


__all__ = ["PinComponent"]
