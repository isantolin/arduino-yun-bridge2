from __future__ import annotations

from enum import IntEnum
from typing import Any, Protocol
from collections.abc import AsyncIterator


class MqttError(Exception):
    ...


class ProtocolVersion(IntEnum):
    V31 = 3
    V311 = 4
    V5 = 5


class _MessageManager(Protocol):
    async def __aenter__(self) -> AsyncIterator[Any]: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None: ...

    def __aiter__(self) -> AsyncIterator[Any]: ...


class Client:
    def __init__(
        self,
        *,
        hostname: str,
        port: int,
        username: str | None = ...,
        password: str | None = ...,
        tls_context: Any | None = ...,
        logger: Any | None = ...,
        protocol: ProtocolVersion | None = ...,
        clean_session: bool | None = ...,
    ) -> None: ...

    async def __aenter__(self) -> Client: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None: ...

    async def connect(self, *args: Any, **kwargs: Any) -> None: ...

    async def disconnect(self, *args: Any, **kwargs: Any) -> None: ...

    def messages(self) -> _MessageManager: ...

    def unfiltered_messages(self) -> _MessageManager: ...

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
        properties: Any | None = ...,
    ) -> None: ...

    async def subscribe(self, topic: str, qos: int = ...) -> None: ...

    async def unsubscribe(self, topic: str) -> None: ...


__all__ = ["Client", "MqttError", "ProtocolVersion"]
