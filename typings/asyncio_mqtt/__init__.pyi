from __future__ import annotations

from typing import AsyncContextManager, AsyncIterator, Optional, Sequence, Tuple

import paho.mqtt.client as mqtt


class Client:
    hostname: str
    port: int

    def __init__(
        self,
        *,
        hostname: str,
        port: int,
        username: Optional[str] = ...,
        password: Optional[str] = ...,
        tls_context: Optional[object] = ...,
        client_id: Optional[str] = ...,
        keepalive: int = ...,
        logger: Optional[object] = ...,
    ) -> None: ...

    async def connect(self, *, timeout: Optional[float] = ...) -> None: ...

    async def disconnect(self, *, timeout: Optional[float] = ...) -> None: ...

    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int,
        retain: bool,
        *,
        timeout: Optional[float] = ...,
    ) -> None: ...

    async def subscribe(
        self,
        topics: Sequence[Tuple[str, int]],
        *,
        timeout: Optional[float] = ...,
    ) -> None: ...

    async def unsubscribe(
        self,
        topics: Sequence[str],
        *,
        timeout: Optional[float] = ...,
    ) -> None: ...

    def unfiltered_messages(
        self,
    ) -> AsyncContextManager[AsyncIterator[mqtt.MQTTMessage]]: ...
