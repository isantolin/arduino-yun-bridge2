"""SIL-2 GPIO Service for Arduino MCU Bridge: Subscriptions, Streaming, and Telemetry."""

from __future__ import annotations

import time
from typing import Any, Protocol

import structlog

from ..config.const import PROP_KEY_BRIDGE_PIN
from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import CLOUD_EXPIRY_PIN, Command, REGISTRY, Topic
from ..protocol.structures import create_queued_publish

from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.gpio")


class GpioRuntime(Protocol):
    """Protocol for runtime link dependency in GpioService."""

    @property
    def serial(self) -> Any: ...

    @property
    def state(self) -> RuntimeState: ...

    async def enqueue_cloud(self, message: pb.CloudQueuedPublish, *, reply_context: Any | None = None) -> None: ...


class GpioService:
    """Manages reactive MCU pin subscriptions, edge/hysteresis change streams, and telemetry. [SIL-2]"""

    def __init__(self, runtime: GpioRuntime) -> None:
        self._runtime: GpioRuntime = runtime

    async def subscribe_pin(
        self,
        pin: int,
        mode: str = "INPUT",
        interval_ms: int = 50,
        hysteresis: int = 1,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Register or update a hardware pin subscription on the MCU."""
        serial = self._runtime.serial
        if serial is None or not self._runtime.state.is_connected:
            return {"status": "error", "message": "Serial transport is not connected"}

        pb_mode = getattr(pb.PinModeType, f"PIN_{mode.upper()}", pb.PinModeType.PIN_INPUT)
        req = pb.PinSubscribeRequest(
            pin=pin,
            mode=pb_mode,
            interval_ms=interval_ms,
            hysteresis=hysteresis,
            enabled=enabled,
        )

        res = await serial.send(Command.CMD_PIN_SUBSCRIBE.value, req)
        if isinstance(res, pb.PinSubscribeResponse) and res.success:
            state = self._runtime.state
            if enabled:
                state.pin_subscriptions[pin] = {
                    "mode": mode.upper(),
                    "interval_ms": interval_ms,
                    "hysteresis": hysteresis,
                    "subscribed_at": time.time(),
                }
            else:
                state.pin_subscriptions.pop(pin, None)
            return {"status": "ok", "pin": pin, "enabled": enabled}

        return {"status": "error", "message": "MCU rejected pin subscription"}

    async def on_subscribe_resp(self, _seq: int, p: pb.PinSubscribeResponse) -> None:
        """Handle MCU pin subscription confirmation frame."""
        logger.info("MCU confirmed pin subscription", pin=p.pin, success=p.success)

    async def handle_pin_update_event(
        self, _seq_or_event: int | pb.PinUpdateEvent, event: pb.PinUpdateEvent | None = None
    ) -> None:
        """Handle streaming pin value change events from the MCU.

        [SIL-2] Routes through enqueue_cloud() for proper LMDB spool persistence,
        IPC correlation, console fanout, drop metrics, and topic_authorization.
        """
        actual_event = _seq_or_event if isinstance(_seq_or_event, pb.PinUpdateEvent) else (event or pb.PinUpdateEvent())
        state = self._runtime.state
        state.pin_events_count += 1

        logger.debug(
            "Pin update event received",
            pin=actual_event.pin,
            value=actual_event.value,
            timestamp_micros=actual_event.timestamp_micros,
        )

        # [SIL-2] Use canonical topic_path and enqueue_cloud for full pipeline parity
        await self._runtime.enqueue_cloud(
            create_queued_publish(
                REGISTRY.topic_path(state.cloud_topic_prefix, Topic.DIGITAL, str(actual_event.pin), "update"),
                str(actual_event.value).encode("utf-8"),
                message_expiry_interval=CLOUD_EXPIRY_PIN,
            )
        )

    async def handle_digital_read_resp(self, _seq: int, p: pb.DigitalReadResponse) -> None:
        """Handle digital read response frame from MCU."""
        req = self._runtime.state.pending_digital_reads.popleft() if self._runtime.state.pending_digital_reads else None
        pin_str = str(req.pin) if req else "unknown"
        await self._runtime.enqueue_cloud(
            create_queued_publish(
                REGISTRY.topic_path(self._runtime.state.cloud_topic_prefix, Topic.DIGITAL, pin_str, "value"),
                str(p.value).encode("utf-8"),
                message_expiry_interval=CLOUD_EXPIRY_PIN,
                user_properties=((PROP_KEY_BRIDGE_PIN, pin_str),),
                reply_context=req.reply_context if req else None,
            ),
            reply_context=req.reply_context if req else None,
        )

    async def handle_analog_read_resp(self, _seq: int, p: pb.AnalogReadResponse) -> None:
        """Handle analog read response frame from MCU."""
        req = self._runtime.state.pending_analog_reads.popleft() if self._runtime.state.pending_analog_reads else None
        pin_str = str(req.pin) if req else "unknown"
        await self._runtime.enqueue_cloud(
            create_queued_publish(
                REGISTRY.topic_path(self._runtime.state.cloud_topic_prefix, Topic.ANALOG, pin_str, "value"),
                str(p.value).encode("utf-8"),
                message_expiry_interval=CLOUD_EXPIRY_PIN,
                user_properties=((PROP_KEY_BRIDGE_PIN, pin_str),),
                reply_context=req.reply_context if req else None,
            ),
            reply_context=req.reply_context if req else None,
        )
