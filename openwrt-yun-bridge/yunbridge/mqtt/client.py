from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, AsyncIterator, Dict, List, Optional, Self, Sequence, Tuple

import paho.mqtt.client as mqtt

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


class _MessageStream:
    def __init__(self, queue: "asyncio.Queue[DeliveredMessage]") -> None:
        self._queue = queue

    def __aiter__(self) -> AsyncIterator[DeliveredMessage]:
        return self

    async def __anext__(self) -> DeliveredMessage:
        message = await self._queue.get()
        return message


class _MessageContext:
    def __init__(self, queue: "asyncio.Queue[DeliveredMessage]") -> None:
        self._queue = queue

    async def __aenter__(self) -> _MessageStream:
        return _MessageStream(self._queue)

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[Any],
    ) -> None:
        return None


class Client:
    _CONNECT_TIMEOUT = 15
    _SUBSCRIPTION_TIMEOUT = 10
    _UNSUBSCRIBE_TIMEOUT = 10
    _DISCONNECT_TIMEOUT = 5

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
    ) -> None:
        self._client = mqtt.Client(client_id=client_id or "")
        self._client.enable_logger(logger)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._client.on_unsubscribe = self._on_unsubscribe

        self._message_queue: "asyncio.Queue[DeliveredMessage]" = asyncio.Queue()
        self._connected = False
        self._connect_future: Optional[asyncio.Future[None]] = None
        self._disconnect_future: Optional[asyncio.Future[Optional[Exception]]] = None
        self._pending_subscriptions: Dict[int, asyncio.Future[List[int]]] = {}
        self._pending_unsubscriptions: Dict[int, asyncio.Future[None]] = {}

    @property
    def messages(self) -> _MessageContext:
        return _MessageContext(self._message_queue)

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
        if self._connected:
            raise MQTTError("Client already connected")

        loop = asyncio.get_running_loop()
        self._connect_future = loop.create_future()
        self._disconnect_future = loop.create_future()

        if username is not None:
            self._client.username_pw_set(username, password)
        else:
            # Reset credentials when reconnecting with anonymous access.
            self._client.username_pw_set(None, None)

        if ssl is not None:
            self._client.tls_set_context(ssl)  # type: ignore[arg-type]

        # Use exponential back-off with modest bounds for broker reconnects.
        self._client.reconnect_delay_set(min_delay=1, max_delay=3)
        self._client.connect_async(host=host, port=port, keepalive=keepalive)
        self._client.loop_start()

        try:
            await asyncio.wait_for(self._connect_future, self._CONNECT_TIMEOUT)
        except Exception:
            self._client.loop_stop()
            raise

        return ConnectResult(
            disconnect_reason=self._disconnect_future
        )  # type: ignore[arg-type]

    async def disconnect(self) -> None:
        disconnect_future = self._disconnect_future

        def _do_disconnect() -> None:
            try:
                self._client.disconnect()
            except ValueError:
                # The underlying client raises ValueError when not connected.
                pass

        if self._connected:
            await asyncio.get_running_loop().run_in_executor(None, _do_disconnect)
            if disconnect_future is not None and not disconnect_future.done():
                try:
                    await asyncio.wait_for(
                        disconnect_future,
                        timeout=self._DISCONNECT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Timeout waiting for MQTT disconnect ack")
        else:
            # Ensure the future resolves even when already disconnected.
            if disconnect_future is not None and not disconnect_future.done():
                disconnect_future.set_result(None)

        self._client.loop_stop()
        self._connected = False

    async def publish(self, message: PublishableMessage) -> None:
        if not self._connected:
            raise ConnectionLostError("MQTT client not connected")

        payload = message.payload or b""

        info = self._client.publish(
            topic=message.topic_name,
            payload=payload,
            qos=int(message.qos),
            retain=message.retain,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionLostError(f"MQTT publish failed (rc={info.rc})")

        await asyncio.get_running_loop().run_in_executor(None, info.wait_for_publish)

    async def subscribe(
        self, *subscriptions: Tuple[str, QOSLevel | int]
    ) -> None:
        if not subscriptions:
            return
        if not self._connected:
            raise ConnectionLostError("MQTT client not connected")

        normalized: List[Tuple[str, int]] = []
        for topic, qos in subscriptions:
            qos_value = int(qos)
            normalized.append((topic, qos_value))

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        result, mid = self._client.subscribe(normalized)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionLostError(f"MQTT subscribe failed (rc={result})")
        if mid is None:
            raise ConnectionLostError(
                "MQTT subscribe returned invalid message id"
            )

        self._pending_subscriptions[mid] = future
        try:
            await asyncio.wait_for(future, timeout=self._SUBSCRIPTION_TIMEOUT)
        except asyncio.TimeoutError as exc:
            self._pending_subscriptions.pop(mid, None)
            raise ConnectionLostError("MQTT subscribe timed out") from exc

    async def unsubscribe(self, *topics: str) -> None:
        if not topics:
            return
        if not self._connected:
            raise ConnectionLostError("MQTT client not connected")

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        result, mid = self._client.unsubscribe(list(topics))
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionLostError(f"MQTT unsubscribe failed (rc={result})")
        if mid is None:
            raise ConnectionLostError(
                "MQTT unsubscribe returned invalid message id"
            )

        self._pending_unsubscriptions[mid] = future
        try:
            await asyncio.wait_for(future, timeout=self._UNSUBSCRIBE_TIMEOUT)
        except asyncio.TimeoutError as exc:
            self._pending_unsubscriptions.pop(mid, None)
            raise ConnectionLostError("MQTT unsubscribe timed out") from exc

    async def delivered_messages(self) -> AsyncIterator[DeliveredMessage]:
        while True:
            message = await self._message_queue.get()
            yield message

    # ------------------------------------------------------------------
    # Paho callbacks (executed on the network loop thread)
    # ------------------------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Dict[str, Any],
        rc: int,
        properties: Any = None,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Loop not running

        def _resolve() -> None:
            connect_future = self._connect_future
            if connect_future is None:
                return
            if rc == mqtt.CONNACK_ACCEPTED:
                self._connected = True
                if not connect_future.done():
                    connect_future.set_result(None)
            else:
                exc = self._map_connect_error(rc)
                if not connect_future.done():
                    connect_future.set_exception(exc)
                disconnect_future = self._disconnect_future
                if (
                    disconnect_future is not None
                    and not disconnect_future.done()
                ):
                    disconnect_future.set_result(exc)

        loop.call_soon_threadsafe(_resolve)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        rc: int,
        properties: Any = None,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _resolve() -> None:
            disconnect_future = self._disconnect_future
            self._connected = False
            if disconnect_future is None or disconnect_future.done():
                return
            if rc == mqtt.MQTT_ERR_SUCCESS or rc == 0:
                disconnect_future.set_result(None)
            else:
                disconnect_future.set_result(
                    ConnectionLostError(f"MQTT disconnect rc={rc}")
                )

        loop.call_soon_threadsafe(_resolve)

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        try:
            qos = QOSLevel(message.qos)
        except ValueError:
            qos = QOSLevel.QOS_0

        delivered = DeliveredMessage(
            topic_name=message.topic or "",
            payload=message.payload or b"",
            qos=qos,
            retain=bool(message.retain),
        )

        def _enqueue() -> None:
            self._message_queue.put_nowait(delivered)

        loop.call_soon_threadsafe(_enqueue)

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        granted_qos: Sequence[int],
        properties: Any = None,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _resolve() -> None:
            future = self._pending_subscriptions.pop(mid, None)
            if future is not None and not future.done():
                future.set_result(list(granted_qos))

        loop.call_soon_threadsafe(_resolve)

    def _on_unsubscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        properties: Any = None,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _resolve() -> None:
            future = self._pending_unsubscriptions.pop(mid, None)
            if future is not None and not future.done():
                future.set_result(None)

        loop.call_soon_threadsafe(_resolve)

    @staticmethod
    def _map_connect_error(rc: int) -> MQTTError:
        if rc in (
            mqtt.CONNACK_REFUSED_BAD_USERNAME_PASSWORD,
            mqtt.CONNACK_REFUSED_NOT_AUTHORIZED,
        ):
            return AccessRefusedError(f"MQTT access refused (rc={rc})")
        if rc == mqtt.CONNACK_REFUSED_SERVER_UNAVAILABLE:
            return ConnectionCloseForcedError("MQTT server unavailable")
        return MQTTError(f"MQTT connection failed (rc={rc})")
