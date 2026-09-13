"""SIL-2 GPIO Service for Arduino MCU Bridge: Subscriptions, Streaming, and Telemetry."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import Command, CLOUD_EXPIRY_PIN, Topic
from ..protocol.structures import create_queued_publish
from ..protocol.topics import topic_path

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = structlog.get_logger("mcubridge.gpio")


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

    async def handle_pin_update_event(self, event: pb.PinUpdateEvent) -> None:
        """Handle streaming pin value change events from the MCU.

        [SIL-2] Routes through enqueue_cloud() for proper LMDB spool persistence,
        IPC correlation, console fanout, drop metrics, and topic_authorization.
        """
        state = self._runtime.state
        state.pin_events_count += 1

        logger.debug(
            "Pin update event received",
            pin=event.pin,
            value=event.value,
            timestamp_micros=event.timestamp_micros,
        )

        # [SIL-2] Use canonical topic_path and enqueue_cloud for full pipeline parity
        await self._runtime.enqueue_cloud(
            create_queued_publish(
                topic_path(state.cloud_topic_prefix, Topic.DIGITAL, str(event.pin), "update"),
                str(event.value).encode("utf-8"),
                message_expiry_interval=CLOUD_EXPIRY_PIN,
            )
        )

    def get_subscriptions(self) -> dict[int, dict[str, Any]]:
        """Retrieve list of currently active pin subscriptions."""
        return self._runtime.state.pin_subscriptions
