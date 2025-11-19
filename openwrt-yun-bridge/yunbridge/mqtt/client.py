"""MQTT client shim with YunBridge-specific resilience tweaks."""
from __future__ import annotations

import asyncio
from asyncio import Future
from typing import Any, Callable, Optional, Type, cast

try:  # pragma: no cover - exercised in integration/packaging tests
    from asyncio_mqtt import Client as BaseClient  # type: ignore[import]
    from asyncio_mqtt.error import MqttError  # type: ignore[import]
except Exception as exc:  # pragma: no cover - allows unit tests without deps
    _missing_reason = repr(exc)

    class _MissingMQTTClient:  # pylint: disable=too-few-public-methods
        def __init__(self, *_: Any, **__: Any) -> None:
            raise RuntimeError(
                "asyncio-mqtt is required to use YunBridge MQTT features "
                f"({_missing_reason})."
            )

    BaseClient = cast(Type[Any], _MissingMQTTClient)

    class MqttError(RuntimeError):
        """Fallback error when asyncio-mqtt is unavailable."""

        pass


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
