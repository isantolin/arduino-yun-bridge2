"""SIL-2 GPIO Service for Arduino MCU Bridge: Subscriptions, Streaming, and Telemetry."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import Command

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = structlog.get_logger("mcubridge.gpio")

PIN_MODE_MAP: dict[str, pb.PinModeType] = {
    "INPUT": pb.PinModeType.PIN_INPUT,
    "OUTPUT": pb.PinModeType.PIN_OUTPUT,
    "INPUT_PULLUP": pb.PinModeType.PIN_INPUT_PULLUP,
}


class GpioService:
    """Manages reactive MCU pin subscriptions, edge/hysteresis change streams, and telemetry. [SIL-2]"""

    def __init__(self, runtime: BridgeService) -> None:
        self._runtime: BridgeService = runtime

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

        pb_mode = PIN_MODE_MAP.get(mode.upper(), pb.PinModeType.PIN_INPUT)
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

    async def handle_pin_update_event(self, event: pb.PinUpdateEvent) -> None:
        """Handle streaming pin value change events from the MCU."""
        state = self._runtime.state
        state.pin_events_count += 1

        logger.debug(
            "Pin update event received",
            pin=event.pin,
            value=event.value,
            timestamp_micros=event.timestamp_micros,
        )

        # Forward to cloud gateway publish queue
        cloud_msg = pb.CloudQueuedPublish(
            topic_name=f"gpio/pin_{event.pin}/update",
            payload=str(event.value).encode("utf-8"),
            qos=0,
        )
        try:
            state.cloud_publish_queue.put_nowait(cloud_msg)
        except asyncio.QueueFull:
            state.cloud_dropped_messages += 1

    def get_subscriptions(self) -> dict[int, dict[str, Any]]:
        """Retrieve list of currently active pin subscriptions."""
        return self._runtime.state.pin_subscriptions
