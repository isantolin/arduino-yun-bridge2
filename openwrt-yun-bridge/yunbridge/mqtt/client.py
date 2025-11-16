"""MQTT client shim with YunBridge-specific resilience tweaks."""
from __future__ import annotations

import asyncio
from asyncio import Future
from typing import Any, Callable, Optional, cast

from asyncio_mqtt import Client as BaseClient
from asyncio_mqtt.error import MqttError


class Client(BaseClient):
    """Subclass that suppresses noisy cancellation traces during shutdown."""

    def _on_disconnect(  # type: ignore[override]
        self,
        client: Any,
        userdata: Any,
        rc: int,
        properties: Optional[Any] = None,
    ) -> None:
        connected_future = cast(
            Optional[Future[Any]], getattr(self, "_connected", None)
        )
        if connected_future and connected_future.cancelled():
            disconnected_future = cast(
                Optional[Future[Any]], getattr(self, "_disconnected", None)
            )
            if disconnected_future and not disconnected_future.done():
                disconnected_future.cancel()
            return
        base_disconnect = cast(
            Optional[Callable[[Any, Any, int, Optional[Any]], None]],
            getattr(super(), "_on_disconnect", None),
        )
        if base_disconnect is None:
            return

        try:
            base_disconnect(client, userdata, rc, properties)
        except asyncio.CancelledError:
            disconnected_future = cast(
                Optional[Future[Any]], getattr(self, "_disconnected", None)
            )
            if disconnected_future and not disconnected_future.done():
                disconnected_future.cancel()


MQTTError = MqttError

__all__ = [
    "Client",
    "MQTTError",
]
