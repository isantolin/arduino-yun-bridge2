"""MQTT client shim with YunBridge-specific resilience tweaks."""
from __future__ import annotations

import asyncio
from asyncio import Future
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from aiomqtt import Client as _AiomqttClient  # type: ignore[import]
    from aiomqtt import MqttError as _AiomqttError  # type: ignore[import]
else:  # pragma: no cover - exercised in integration/packaging tests
    try:
        import aiomqtt  # type: ignore[import]
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
            from aiomqtt import error as _aiomqtt_error  # type: ignore[import]

            _AiomqttError = _aiomqtt_error.MqttError


BaseClient = _AiomqttClient
MqttError = _AiomqttError


class Client(BaseClient):
    """Subclass that suppresses noisy cancellation traces during shutdown."""

    def _on_disconnect(  # type: ignore[override]
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


MQTTError = MqttError

__all__ = [
    "Client",
    "MQTTError",
]
