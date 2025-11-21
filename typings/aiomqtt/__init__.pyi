from __future__ import annotations

from typing import Any, AsyncContextManager, AsyncIterator


class Client:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    async def __aenter__(self) -> "Client": ...

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    async def connect(self, *, timeout: float | None = ...) -> None: ...

    async def disconnect(self, *, timeout: float | None = ...) -> None: ...

    async def subscribe(self, *args: Any, **kwargs: Any) -> Any: ...

    async def unsubscribe(self, *args: Any, **kwargs: Any) -> Any: ...

    async def publish(self, *args: Any, **kwargs: Any) -> Any: ...

    def messages(
        self, *args: Any, **kwargs: Any
    ) -> AsyncContextManager[AsyncIterator[Any]]: ...

    def filtered_messages(
        self, *args: Any, **kwargs: Any
    ) -> AsyncContextManager[AsyncIterator[Any]]: ...

    def unfiltered_messages(
        self, *args: Any, **kwargs: Any
    ) -> AsyncContextManager[AsyncIterator[Any]]: ...

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...


class MqttError(Exception):
    ...


__all__ = ["Client", "MqttError"]
