"""SIL-2 GPIO Service for Arduino MCU Bridge: Subscriptions, Streaming, and Telemetry."""

from __future__ import annotations

import time
from typing import Any, Protocol

import structlog

from ..protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from ..protocol.protocol import (
    CLOUD_EXPIRY_PIN,
    DEFAULT_PIN_HYSTERESIS,
    DEFAULT_PIN_INTERVAL_MS,
    MIN_PIN_INTERVAL_MS,
    topic_path,
)
from ..protocol.structures import create_queued_publish

from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.gpio")


class GpioRuntime(Protocol):
    """Protocol for runtime link dependency in GpioService."""

    @property
    def serial(self) -> Any: ...

    @property
    def state(self) -> RuntimeState: ...

    async def enqueue_cloud(
        self, message: cloud_gateway_pb2.CloudQueuedPublish, *, reply_context: Any | None = None
    ) -> None: ...


class GpioService:
    """Manages reactive MCU pin subscriptions, edge/hysteresis change streams, and telemetry. [SIL-2]"""

    def __init__(self, runtime: GpioRuntime) -> None:
        self._runtime: GpioRuntime = runtime

    async def subscribe_pin(
        self,
        pin: int,
        mode: str = "INPUT",
        interval_ms: int = DEFAULT_PIN_INTERVAL_MS,
        hysteresis: int = DEFAULT_PIN_HYSTERESIS,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Register or update a hardware pin subscription on the MCU."""
        serial = self._runtime.serial
        if serial is None or not self._runtime.state.is_connected:
            return {"status": "error", "message": "Serial transport is not connected"}
        if enabled and interval_ms < MIN_PIN_INTERVAL_MS:
            # An interval of 0 makes the MCU report on every loop iteration and floods the link.
            return {"status": "error", "message": f"interval_ms must be >= {MIN_PIN_INTERVAL_MS}"}

        pb_mode = getattr(config_schema_pb2.PinModeType, f"PIN_{mode.upper()}", config_schema_pb2.PinModeType.PIN_INPUT)
        req = mcu_wire_pb2.PinSubscribeRequest(
            pin=pin,
            mode=pb_mode,
            interval_ms=interval_ms,
            hysteresis=hysteresis,
            enabled=enabled,
        )

        if await serial.send("rpc.pb.GpioService/SubscribePins", req):
            state = self._runtime.state
            if enabled:
                state.pin_subscriptions[pin] = {
                    "mode": mode.upper(),
                    "interval_ms": interval_ms,
                    "hysteresis": hysteresis,
                    "subscribed_at": time.time(),
                    "active": True,
                }
            else:
                state.pin_subscriptions.pop(pin, None)
                if not state.pin_subscriptions:
                    serial.cancel_pin_stream()
            return {"status": "ok", "pin": pin, "enabled": enabled}

        return {"status": "error", "message": "MCU rejected pin subscription"}

    async def resubscribe_pins(self) -> int:
        """Re-register active pin subscriptions on the MCU after serial reconnection."""
        serial = self._runtime.serial
        state = self._runtime.state
        if serial is None or not state.is_connected or not state.pin_subscriptions:
            return 0

        restored = 0
        for pin, sub in tuple(state.pin_subscriptions.items()):
            if not sub.get("active", True):
                continue
            mode_str = str(sub.get("mode", "INPUT")).upper()
            pb_mode = getattr(config_schema_pb2.PinModeType, f"PIN_{mode_str}", config_schema_pb2.PinModeType.PIN_INPUT)
            req = mcu_wire_pb2.PinSubscribeRequest(
                pin=pin,
                mode=pb_mode,
                interval_ms=int(sub.get("interval_ms", DEFAULT_PIN_INTERVAL_MS)),
                hysteresis=int(sub.get("hysteresis", DEFAULT_PIN_HYSTERESIS)),
                enabled=True,
            )
            if await serial.send("rpc.pb.GpioService/SubscribePins", req):
                sub["active"] = True
                restored += 1
            else:
                sub["active"] = False
        return restored

    async def handle_pin_update_event(self, event: mcu_wire_pb2.PinUpdateEvent) -> None:
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
                topic_path(state.cloud_topic_prefix, "evt", "GpioService", "PinUpdateEvent", str(event.pin)),
                str(event.value).encode("utf-8"),
                message_expiry_interval=CLOUD_EXPIRY_PIN,
            )
        )
