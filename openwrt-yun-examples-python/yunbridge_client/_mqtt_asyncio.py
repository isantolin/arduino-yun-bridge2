"""Aiomqtt-backed MQTT client with MQTT v5 defaults for Yun Bridge examples."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import (
    Any,
    Awaitable,
    AsyncContextManager,
    AsyncIterator,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

try:
    from aiomqtt import Client as BaseMQTTClient
    from aiomqtt.client import ProtocolVersion
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    raise ModuleNotFoundError(
        "aiomqtt is required to run the Yun Bridge client examples. "
        "Install it with `pip install aiomqtt`."
    ) from exc

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

logger = logging.getLogger("yunbridge_client.mqtt")


class MQTTError(Exception):
    """Base error for MQTT operations."""


class AccessRefusedError(MQTTError):
    """Raised when the broker refuses access (bad credentials)."""


class ConnectionLostError(MQTTError):
    """Raised when the network connection is interrupted."""


class ConnectionCloseForcedError(MQTTError):
    """Raised when the broker closes the connection prematurely."""


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
    content_type: Optional[str] = None
    payload_format_indicator: Optional[int] = None
    message_expiry_interval: Optional[int] = None
    response_topic: Optional[str] = None
    correlation_data: Optional[bytes] = None
    user_properties: Tuple[Tuple[str, str], ...] = ()

    def with_payload(
        self,
        payload: bytes,
        *,
        qos: Optional[QOSLevel] = None,
        retain: Optional[bool] = None,
    ) -> "PublishableMessage":
        return replace(
            self,
            payload=payload,
            qos=qos if qos is not None else self.qos,
            retain=retain if retain is not None else self.retain,
        )

    def with_user_property(self, key: str, value: str) -> "PublishableMessage":
        return replace(
            self,
            user_properties=self.user_properties + ((key, value),),
        )

    def with_user_properties(
        self,
        properties: Mapping[Any, Any] | Iterable[Tuple[Any, Any]],
    ) -> "PublishableMessage":
        if isinstance(properties, Mapping):
            props_iter: Iterable[Tuple[Any, Any]] = cast(
                Iterable[Tuple[Any, Any]],
                properties.items(),
            )
        else:
            props_iter = properties
        combined = tuple((str(key), str(value)) for key, value in props_iter)
        return replace(
            self,
            user_properties=self.user_properties + combined,
        )

    def with_response_topic(
        self, topic: Optional[str]
    ) -> "PublishableMessage":
        return replace(self, response_topic=topic)

    def with_correlation_data(
        self, data: Optional[bytes]
    ) -> "PublishableMessage":
        return replace(self, correlation_data=data)

    def build_properties(self) -> Optional[Properties]:
        if not any(
            [
                self.content_type,
                self.payload_format_indicator is not None,
                self.message_expiry_interval is not None,
                self.response_topic,
                self.correlation_data is not None,
                self.user_properties,
            ]
        ):
            return None

        props = Properties(PacketTypes.PUBLISH)
        if self.content_type is not None:
            props.content_type = self.content_type
        if self.payload_format_indicator is not None:
            props.payload_format_indicator = (
                self.payload_format_indicator
            )
        if self.message_expiry_interval is not None:
            props.message_expiry_interval = int(
                self.message_expiry_interval
            )
        if self.response_topic is not None:
            props.response_topic = self.response_topic
        if self.correlation_data is not None:
            props.correlation_data = self.correlation_data
        if self.user_properties:
            props.user_property = list(self.user_properties)
        return props


@dataclass(slots=True)
class DeliveredMessage:
    topic_name: str
    payload: bytes
    qos: QOSLevel
    retain: bool
    correlation_data: Optional[bytes] = None
    user_properties: Tuple[Tuple[str, str], ...] = ()
    content_type: Optional[str] = None
    message_expiry_interval: Optional[int] = None


@dataclass(slots=True)
class ConnectResult:
    disconnect_reason: asyncio.Future[Optional[Exception]]


class Client:
    _CONNECT_TIMEOUT = 15
    _SUBSCRIPTION_TIMEOUT = 10
    _UNSUBSCRIBE_TIMEOUT = 10
    _DISCONNECT_TIMEOUT = 5

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        hostname: str = "127.0.0.1",
        port: int = 8883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tls_context: Optional[Any] = None,
        keepalive: int = 60,
        logger_obj: Optional[logging.Logger] = None,
        **extra: Any,
    ) -> None:
        self._client_id = client_id
        self._client: Optional[Any] = None
        self._message_cm: Optional[
            AsyncContextManager[AsyncIterator[Any]]
        ] = None
        self._message_gen: Optional[AsyncIterator[Any]] = None
        self._disconnect_future: Optional[
            asyncio.Future[Optional[Exception]]
        ] = None
        self._disconnect_task: Optional[asyncio.Task[None]] = None
        base_kwargs: Dict[str, Any] = {
            "hostname": hostname,
            "port": port,
            "username": username,
            "password": password,
            "tls_context": tls_context,
            "keepalive": keepalive,
            "logger": logger_obj,
        }
        base_kwargs.update(extra)
        self._client_kwargs: Dict[str, Any] = {}
        self._store_client_kwargs(**base_kwargs)

    def _store_client_kwargs(self, **kwargs: Any) -> None:
        merged = {k: v for k, v in kwargs.items() if v is not None}
        if self._client_id is not None:
            merged.setdefault("client_id", self._client_id)
        merged.setdefault("protocol", ProtocolVersion.V5)
        merged.setdefault(
            "clean_start", mqtt.MQTT_CLEAN_START_FIRST_ONLY
        )
        if merged.get("logger") is None:
            merged["logger"] = logger
        self._client_kwargs = merged

    async def connect(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ssl: Optional[object] = None,
        keepalive: Optional[int] = None,
        timeout: Optional[float] = None,
        **overrides: Any,
    ) -> ConnectResult:
        if self._client is not None:
            raise MQTTError("Client already connected")

        kwargs = dict(self._client_kwargs)
        if host is not None:
            kwargs["hostname"] = host
        if port is not None:
            kwargs["port"] = port
        if username is not None:
            kwargs["username"] = username
        if password is not None:
            kwargs["password"] = password
        if ssl is not None:
            kwargs["tls_context"] = ssl
        if keepalive is not None:
            kwargs["keepalive"] = keepalive
        kwargs.update(overrides)

        properties = kwargs.pop("properties", None)
        if properties is None:
            properties = _build_mqtt_connect_properties()

        client_kwargs: Dict[str, Any] = dict(kwargs)
        client_kwargs["properties"] = properties
        self._client = BaseMQTTClient(**client_kwargs)
        self._store_client_kwargs(**kwargs)

        try:
            connect_timeout = (
                timeout if timeout is not None else self._CONNECT_TIMEOUT
            )
            await self._client.connect(timeout=connect_timeout)
        except Exception as exc:
            if hasattr(exc, "rc"):
                raise self._map_connect_error(exc) from exc
            raise ConnectionLostError(str(exc)) from exc

        loop = asyncio.get_running_loop()
        self._disconnect_future = loop.create_future()
        self._disconnect_task = loop.create_task(self._watch_disconnect())

        messages_factory = getattr(self._client, "messages", None)
        if messages_factory is not None:
            self._message_cm = cast(
                AsyncContextManager[AsyncIterator[Any]],
                messages_factory(),
            )
        else:
            self._message_cm = cast(
                AsyncContextManager[AsyncIterator[Any]],
                self._client.unfiltered_messages(),
            )
        self._message_gen = await self._message_cm.__aenter__()

        return ConnectResult(disconnect_reason=self._disconnect_future)

    async def disconnect(self, *, timeout: Optional[float] = None) -> None:
        disconnect_future = self._disconnect_future
        client = self._client
        if client is None:
            return

        try:
            disconnect_timeout = (
                timeout if timeout is not None else self._DISCONNECT_TIMEOUT
            )
            await client.disconnect(timeout=disconnect_timeout)
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

    async def publish(
        self,
        topic: Union[str, PublishableMessage],
        payload: Union[bytes, bytearray, memoryview, str, None] = None,
        qos: int = 0,
        retain: bool = False,
        *,
        timeout: Optional[float] = None,
        properties: Optional[Properties] = None,
        **kwargs: Any,
    ) -> None:
        client = self._ensure_client()
        try:
            publish_timeout = (
                timeout if timeout is not None else self._CONNECT_TIMEOUT
            )
            if isinstance(topic, PublishableMessage):
                message = topic
                properties = message.build_properties()
                await client.publish(
                    message.topic_name,
                    payload=message.payload,
                    qos=int(message.qos),
                    retain=message.retain,
                    properties=properties,
                    timeout=publish_timeout,
                    **kwargs,
                )
            else:
                await client.publish(
                    topic,
                    payload=_coerce_payload_bytes(payload),
                    qos=qos,
                    retain=retain,
                    properties=properties,
                    timeout=publish_timeout,
                    **kwargs,
                )
        except Exception as exc:
            raise ConnectionLostError(str(exc)) from exc

    async def subscribe(
        self,
        topic: Union[str, Sequence[Tuple[str, int]]],
        qos: int = 0,
        *,
        timeout: Optional[float] = None,
        options: Any = None,
        properties: Optional[Properties] = None,
        **kwargs: Any,
    ) -> None:
        client = self._ensure_client()
        try:
            subscribe_timeout = (
                timeout if timeout is not None else self._SUBSCRIPTION_TIMEOUT
            )
            await client.subscribe(
                topic,
                qos=qos,
                options=options,
                properties=properties,
                timeout=subscribe_timeout,
                **kwargs,
            )
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
                topic_obj = getattr(message, "topic", None)
                topic = str(topic_obj) if topic_obj is not None else ""
                payload_bytes = _coerce_payload_bytes(
                    getattr(message, "payload", None)
                )
                qos_value = getattr(message, "qos", 0)
                try:
                    qos = QOSLevel(int(qos_value))
                except (ValueError, TypeError):
                    qos = QOSLevel.QOS_0
                properties_obj = getattr(message, "properties", None)
                correlation_data: Optional[bytes] = None
                user_props: Tuple[Tuple[str, str], ...] = ()
                content_type = None
                expiry: Optional[int] = None
                if properties_obj is not None:
                    raw_corr = getattr(
                        properties_obj,
                        "CorrelationData",
                        None,
                    )
                    if raw_corr is not None:
                        correlation_data = (
                            bytes(raw_corr)
                            if not isinstance(raw_corr, bytes)
                            else raw_corr
                        )
                    raw_user = getattr(
                        properties_obj,
                        "UserProperty",
                        None,
                    )
                    if raw_user:
                        user_props = tuple(
                            (str(key), str(value))
                            for key, value in raw_user
                        )
                    content_type = getattr(
                        properties_obj,
                        "ContentType",
                        None,
                    )
                    expiry = getattr(
                        properties_obj,
                        "MessageExpiryInterval",
                        None,
                    )
                yield DeliveredMessage(
                    topic_name=topic,
                    payload=payload_bytes,
                    qos=qos,
                    retain=bool(getattr(message, "retain", False)),
                    correlation_data=correlation_data,
                    user_properties=user_props,
                    content_type=content_type,
                    message_expiry_interval=expiry,
                )
        except Exception as exc:
            raise ConnectionLostError(str(exc)) from exc

    def unfiltered_messages(self) -> AsyncContextManager[AsyncIterator[Any]]:
        client = self._ensure_client()
        return cast(
            AsyncContextManager[AsyncIterator[Any]],
            client.unfiltered_messages(),
        )

    def _ensure_client(self) -> Any:
        if self._client is None:
            raise MQTTError("MQTT client not connected")
        return self._client

    async def _cleanup_message_stream(self) -> None:
        if self._message_cm is not None:
            try:
                await self._message_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug(
                    "Ignoring error when closing MQTT message stream"
                )
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
            await cast(Awaitable[object], raw_future)
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
        reason = getattr(exc, "reason_code", None)
        if reason is not None and hasattr(reason, "value"):
            rc = getattr(reason, "value", rc)
        if rc in (
            mqtt.CONNACK_REFUSED_BAD_USERNAME_PASSWORD,
            mqtt.CONNACK_REFUSED_NOT_AUTHORIZED,
        ):
            return AccessRefusedError(f"MQTT access refused (rc={rc})")
        if rc == mqtt.CONNACK_REFUSED_SERVER_UNAVAILABLE:
            return ConnectionCloseForcedError("MQTT server unavailable")
        return ConnectionLostError(f"MQTT connection failed (rc={rc})")


def _set_mqtt_property(props: Properties, camel_name: str, value: int) -> None:
    try:
        setattr(props, camel_name, value)
    except AttributeError as exc:
        raise RuntimeError(
            f"paho-mqtt missing MQTT v5 property '{camel_name}'"
        ) from exc


def _build_mqtt_connect_properties() -> Properties:
    props = Properties(PacketTypes.CONNECT)
    _set_mqtt_property(props, "SessionExpiryInterval", 0)
    _set_mqtt_property(props, "RequestResponseInformation", 1)
    _set_mqtt_property(props, "RequestProblemInformation", 1)
    return props


def _coerce_payload_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    try:
        return bytes(value)
    except Exception:  # pragma: no cover - defensive fallback
        return str(value).encode("utf-8", errors="ignore")


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
