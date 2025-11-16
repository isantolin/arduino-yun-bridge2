from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import (
    AsyncContextManager,
    AsyncIterator,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TYPE_CHECKING,
    cast,
)

import paho.mqtt.client as mqtt

if TYPE_CHECKING:
    from asyncio_mqtt import Client as AsyncioMqttClient
    from asyncio_mqtt.error import MqttConnectError, MqttError
else:  # pragma: no cover - import at runtime with graceful fallback
    try:
        from asyncio_mqtt import (  # type: ignore[import]
            Client as AsyncioMqttClient,
        )
        from asyncio_mqtt.error import (  # type: ignore[import]
            MqttConnectError,
            MqttError,
        )
    except ImportError:  # pragma: no cover - optional dependency missing
        AsyncioMqttClient = None  # type: ignore[assignment]

        class MqttError(Exception):  # type: ignore[override]
            pass

        class MqttConnectError(MqttError):  # type: ignore[override]
            def __init__(self, rc: int = 0, *args: object) -> None:
                super().__init__(*args)
                self.rc = rc


class _MQTTAsyncClient(Protocol):
    async def connect(self, timeout: Optional[float] = ...) -> None: ...

    async def disconnect(self, timeout: Optional[float] = ...) -> None: ...

    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int,
        retain: bool,
        timeout: Optional[float] = ...,
    ) -> None: ...

    async def subscribe(
        self,
        topics: Sequence[Tuple[str, int]],
        timeout: Optional[float] = ...,
    ) -> None: ...

    async def unsubscribe(
        self,
        topics: Sequence[str],
        timeout: Optional[float] = ...,
    ) -> None: ...

    def unfiltered_messages(
        self,
    ) -> AsyncContextManager[AsyncIterator[mqtt.MQTTMessage]]: ...


logger = logging.getLogger("yunbridge.mqtt")


class MQTTError(Exception):
    """Base error for MQTT operations."""


class AccessRefusedError(MQTTError):
    """Raised when the broker refuses access (bad credentials)."""


class ConnectionLostError(MQTTError):
    """Raised when the network connection is interrupted."""


class ConnectionCloseForcedError(MQTTError):
    """Raised when the broker closes the connection before establishment."""


class QOSLevel(IntEnum):
    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


@dataclass(slots=True)
class PublishableMessage:
    topic_name: str
    payload: bytes
    qos: QOSLevel = QOSLevel.QOS_0
    retain: bool = False


@dataclass(slots=True)
class DeliveredMessage:
    topic_name: str
    payload: bytes
    qos: QOSLevel
    retain: bool


@dataclass(slots=True)
class ConnectResult:
    disconnect_reason: asyncio.Future[Optional[Exception]]


class Client:
    _CONNECT_TIMEOUT = 15
    _SUBSCRIPTION_TIMEOUT = 10
    _UNSUBSCRIBE_TIMEOUT = 10
    _DISCONNECT_TIMEOUT = 5

    def __init__(self, *, client_id: Optional[str] = None) -> None:
        self._client_id = client_id
        self._client: Optional[_MQTTAsyncClient] = None
        self._message_cm: Optional[
            AsyncContextManager[AsyncIterator[mqtt.MQTTMessage]]
        ] = None
        self._message_gen: Optional[AsyncIterator[mqtt.MQTTMessage]] = None
        self._disconnect_future: Optional[
            asyncio.Future[Optional[Exception]]
        ] = None
        self._disconnect_task: Optional[asyncio.Task[None]] = None

    async def connect(
        self,
        *,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ssl: Optional[object] = None,
        keepalive: int = 60,
    ) -> ConnectResult:
        if AsyncioMqttClient is None:
            raise MQTTError(
                "asyncio-mqtt dependency not available. Install "
                "python3-asyncio-mqtt or include it in the firmware image."
            )
        if self._client is not None:
            raise MQTTError("Client already connected")

        self._client = cast(
            _MQTTAsyncClient,
            AsyncioMqttClient(
                hostname=host,
                port=port,
                username=username,
                password=password,
                tls_context=ssl,  # type: ignore[arg-type]
                client_id=self._client_id,
                keepalive=keepalive,
                logger=logger,
            ),
        )

        try:
            await self._client.connect(timeout=self._CONNECT_TIMEOUT)
        except MqttConnectError as exc:
            raise self._map_connect_error(cast(object, exc)) from exc
        except Exception as exc:
            raise ConnectionLostError(str(exc)) from exc

        loop = asyncio.get_running_loop()
        self._disconnect_future = loop.create_future()
        self._disconnect_task = loop.create_task(self._watch_disconnect())

        self._message_cm = self._client.unfiltered_messages()
        self._message_gen = await self._message_cm.__aenter__()

        return ConnectResult(disconnect_reason=self._disconnect_future)

    async def disconnect(self) -> None:
        disconnect_future = self._disconnect_future
        client = self._client
        if client is None:
            return

        try:
            await client.disconnect(timeout=self._DISCONNECT_TIMEOUT)
        except Exception as exc:
            logger.debug("Ignoring MQTT disconnect error: %s", exc)
        finally:
            await self._cleanup_message_stream()
            if self._disconnect_task is not None:
                self._disconnect_task.cancel()
                await asyncio.gather(
                    self._disconnect_task, return_exceptions=True
                )
                self._disconnect_task = None
            if disconnect_future is not None and not disconnect_future.done():
                disconnect_future.set_result(None)
            self._client = None
            self._disconnect_future = None

    async def publish(self, message: PublishableMessage) -> None:
        client = self._ensure_client()
        try:
            await client.publish(
                message.topic_name,
                payload=message.payload or b"",
                qos=int(message.qos),
                retain=message.retain,
                timeout=self._CONNECT_TIMEOUT,
            )
        except Exception as exc:
            raise ConnectionLostError(str(exc)) from exc

    async def subscribe(
        self, *subscriptions: Tuple[str, QOSLevel | int]
    ) -> None:
        if not subscriptions:
            return
        client = self._ensure_client()
        topics = [(topic, int(qos)) for topic, qos in subscriptions]
        try:
            await client.subscribe(topics, timeout=self._SUBSCRIPTION_TIMEOUT)
        except Exception as exc:
            raise ConnectionLostError(str(exc)) from exc

    async def unsubscribe(self, *topics: str) -> None:
        if not topics:
            return
        client = self._ensure_client()
        try:
            await client.unsubscribe(
                list(topics), timeout=self._UNSUBSCRIBE_TIMEOUT
            )
        except Exception as exc:
            raise ConnectionLostError(str(exc)) from exc

    async def delivered_messages(self) -> AsyncIterator[DeliveredMessage]:
        if self._message_gen is None:
            raise MQTTError("Client not connected")
        try:
            async for message in self._message_gen:
                try:
                    qos = QOSLevel(message.qos)
                except ValueError:
                    qos = QOSLevel.QOS_0
                yield DeliveredMessage(
                    topic_name=message.topic or "",
                    payload=message.payload or b"",
                    qos=qos,
                    retain=bool(message.retain),
                )
        except Exception as exc:
            raise ConnectionLostError(str(exc)) from exc

    def _ensure_client(self) -> _MQTTAsyncClient:
        if self._client is None:
            raise MQTTError("MQTT client not connected")
        return self._client

    async def _cleanup_message_stream(self) -> None:
        if self._message_cm is not None:
            try:
                await self._message_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("Ignoring error when closing MQTT message stream")
        self._message_cm = None
        self._message_gen = None

    async def _watch_disconnect(self) -> None:
        client = self._ensure_client()
        disconnect_future = self._disconnect_future
        try:
            raw_future = getattr(cast(object, client), "_disconnected", None)
            if raw_future is None:
                await asyncio.Future()
                return
            await raw_future  # type: ignore[awaitable-return]
            if disconnect_future is not None and not disconnect_future.done():
                disconnect_future.set_result(None)
        except Exception as exc:  # pragma: no cover - defensive
            mapped = ConnectionLostError(str(exc))
            if disconnect_future is not None and not disconnect_future.done():
                disconnect_future.set_result(mapped)
        finally:
            await self._cleanup_message_stream()

    @staticmethod
    def _map_connect_error(exc: object) -> MQTTError:
        rc = getattr(exc, "rc", None)
        if rc in (
            mqtt.CONNACK_REFUSED_BAD_USERNAME_PASSWORD,
            mqtt.CONNACK_REFUSED_NOT_AUTHORIZED,
        ):
            return AccessRefusedError(f"MQTT access refused (rc={rc})")
        if rc == mqtt.CONNACK_REFUSED_SERVER_UNAVAILABLE:
            return ConnectionCloseForcedError("MQTT server unavailable")
        return ConnectionLostError(f"MQTT connection failed (rc={rc})")


__all__ = [
    "Client",
    "ConnectResult",
    "PublishableMessage",
    "DeliveredMessage",
    "QOSLevel",
    "MQTTError",
    "AccessRefusedError",
    "ConnectionLostError",
    "ConnectionCloseForcedError",
]

