from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Tuple


class MqttError(Exception):
    ...


class QOSLevel:
    QOS_0: "QOSLevel"


class PublishableMessage:
    topic_name: str
    payload: bytes
    qos: QOSLevel
    retain: bool

    def __init__(
        self,
        topic_name: str,
        payload: bytes,
        qos: QOSLevel = ...,
        retain: bool = ...,
    ) -> None:
        ...


class Message:
    topic: Any
    topic_name: str
    payload: bytes | None


class MessageManager:
    async def __aenter__(self) -> "MessageManager":
        ...

    async def __aexit__(self, *_: Any) -> None:
        ...

    def __aiter__(self) -> AsyncIterator[Message]:
        ...


class ConnectResult:
    disconnect_reason: asyncio.Future[Exception | None] | None


class Client:
    def __init__(self, *, loop: asyncio.AbstractEventLoop | None = ...) -> None:
        ...

    messages: MessageManager

    async def connect(
        self,
        *,
        host: str,
        port: int,
        username: str | None = ...,
        password: str | None = ...,
        ssl: Any | None = ...,
    ) -> ConnectResult:
        ...

    async def disconnect(self) -> Any:
        ...

    async def subscribe(self, *topics: Tuple[str, QOSLevel]) -> Any:
        ...

    async def unsubscribe(self, *topics: str) -> Any:
        ...

    async def publish(self, message: PublishableMessage) -> Any:
        ...

    def delivered_messages(self) -> AsyncIterator[Message]:
        ...
