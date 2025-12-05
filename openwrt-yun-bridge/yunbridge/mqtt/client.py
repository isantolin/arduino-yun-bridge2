"""MQTT client shim with YunBridge-specific resilience tweaks."""
from __future__ import annotations

import asyncio
from asyncio import Future
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, cast

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from aiomqtt import Client as _AiomqttClient
    from aiomqtt import MqttError as _AiomqttError
else:  # pragma: no cover - exercised in integration/packaging tests
    try:
        import aiomqtt
    except Exception as exc:  # pragma: no cover
        # Allow unit tests to run without aiomqtt installed.
        _missing_reason = repr(exc)

        class _AiomqttClient:  # pylint: disable=too-few-public-methods
            def __init__(self, *_: Any, **__: Any) -> None:
                raise RuntimeError(
                    "aiomqtt is required to use YunBridge MQTT "
                    f"features ({_missing_reason})."
                )

        class _AiomqttError(RuntimeError):
            """Fallback error when aiomqtt is unavailable."""

            pass
    else:
        _AiomqttClient = aiomqtt.Client
        _AiomqttError = getattr(aiomqtt, "MqttError", None)
        if _AiomqttError is None:  # pragma: no cover
            # aiomqtt layout may expose MqttError in a submodule.
            from aiomqtt import error as _aiomqtt_error

            _AiomqttError = _aiomqtt_error.MqttError


BaseClient = _AiomqttClient
MqttError = _AiomqttError


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
        connect_fn = getattr(super(), "connect", None)
        if callable(connect_fn):
            if timeout is not None and "timeout" not in kwargs:
                kwargs = dict(kwargs)
                kwargs["timeout"] = timeout
            await cast(
                Callable[..., Awaitable[Any]],
                connect_fn,
            )(*args, **kwargs)
        else:
            await super().__aenter__()
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
            disconnect_fn = getattr(super(), "disconnect", None)
            if callable(disconnect_fn):
                if timeout is not None and "timeout" not in kwargs:
                    kwargs = dict(kwargs)
                    kwargs["timeout"] = timeout
                await cast(
                    Callable[..., Awaitable[Any]],
                    disconnect_fn,
                )(*args, **kwargs)
            else:
                await super().__aexit__(None, None, None)
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
        base = super()
        stream_factory = getattr(base, "unfiltered_messages", None)
        if callable(stream_factory):
            return stream_factory()
        legacy_factory = getattr(base, "messages", None)
        if callable(legacy_factory):
            return legacy_factory()
        raise AttributeError("MQTT client does not provide a message stream API")


MQTTError = MqttError

__all__ = [
    "Client",
    "MQTTError",
]
