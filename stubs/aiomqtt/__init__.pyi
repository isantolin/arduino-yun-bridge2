from __future__ import annotations

from typing import Any, AsyncIterator, Protocol


class MqttError(Exception): ...


class _MessageManager(Protocol):
    def __aiter__(self) -> AsyncIterator[Any]: ...


class Client:
    async def __aenter__(self) -> Client: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None: ...

    async def connect(self, *args: Any, **kwargs: Any) -> None: ...

    async def disconnect(self, *args: Any, **kwargs: Any) -> None: ...

    def unfiltered_messages(self) -> _MessageManager: ...


__all__ = ["Client", "MqttError"]
