"""MQTT client adapter with YunBridge-specific resilience tweaks."""
from __future__ import annotations

import asyncio
from asyncio import Future
from typing import Any, Callable, Optional, cast

from aiomqtt import Client as BaseClient, MqttError


class Client(BaseClient):
    """Subclass that adds explicit connect/disconnect helpers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._entered_context = False

    async def connect(
        self,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Provide an explicit connect hook for the daemon."""

        if self._entered_context:
            return
        if timeout is not None and "timeout" not in kwargs:
            kwargs = dict(kwargs)
            kwargs["timeout"] = timeout
        await super().connect(*args, **kwargs)
        self._entered_context = True

    async def disconnect(
        self,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        if not self._entered_context:
            return
        try:
            if timeout is not None and "timeout" not in kwargs:
                kwargs = dict(kwargs)
                kwargs["timeout"] = timeout
            await super().disconnect(*args, **kwargs)
        finally:
            self._entered_context = False

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        *args: Any,
        **kwargs: Any,
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
        base_proxy: Any = super()
        base_disconnect = cast(
            Optional[Callable[..., None]],
            getattr(base_proxy, "_on_disconnect", None),
        )
        if base_disconnect is None:
            return

        try:
            base_disconnect(client, userdata, *args, **kwargs)
        except asyncio.CancelledError:
            disconnected_future = cast(
                Optional[Future[Any]], getattr(self, "_disconnected", None)
            )
            if disconnected_future and not disconnected_future.done():
                disconnected_future.cancel()

    def unfiltered_messages(self) -> Any:
        return super().unfiltered_messages()


MQTTError = MqttError

__all__ = [
    "Client",
    "MQTTError",
]
