"""Async MQTT helpers for Yun Bridge example scripts."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shlex
import uuid
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    TypedDict,
    Union,
    cast,
)

from .env import dump_client_env
from yunbridge.const import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
)
from yunbridge.mqtt import (
    InboundMessage,
    PublishableMessage,
    QOSLevel,
    as_inbound_message,
    MQTTClient,
    MQTTError,
)

Client = MQTTClient
AiomqttClient = MQTTClient

__all__ = [
    "Bridge",
    "dump_client_env",
    "Client",
    "MQTTError",
    "QOSLevel",
]

MQTT_HOST = os.environ.get("YUN_BROKER_IP", DEFAULT_MQTT_HOST)
MQTT_PORT = int(os.environ.get("YUN_BROKER_PORT", DEFAULT_MQTT_PORT))
MQTT_TOPIC_PREFIX = DEFAULT_MQTT_TOPIC
MQTT_USER = os.environ.get("YUN_BROKER_USER")
MQTT_PASS = os.environ.get("YUN_BROKER_PASS")

logger = logging.getLogger(__name__)

_PublishPayload = TypeVar("_PublishPayload", str, bytes)


class ShellPollResponse(TypedDict, total=False):
    stdout: str
    stderr: str
    stdout_base64: str
    stderr_base64: str
    stdout_truncated: bool
    stderr_truncated: bool
    finished: bool
    exit_code: int


def _format_shell_command(parts: Sequence[str]) -> str:
    if not parts:
        raise ValueError("command_parts must not be empty")
    return " ".join(shlex.quote(part) for part in parts)


async def _subscribe_many(client: AiomqttClient, topics: Sequence[str]) -> None:
    for topic in topics:
        await client.subscribe(topic, qos=int(QOSLevel.QOS_0))


async def _unsubscribe_many(client: AiomqttClient, topics: Sequence[str]) -> None:
    for topic in topics:
        await client.unsubscribe(topic)


async def _publish_simple(
    client: AiomqttClient,
    topic: str,
    payload: _PublishPayload,
    *,
    retain: bool = False,
) -> None:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    await client.publish(
        topic,
        data,
        qos=int(QOSLevel.QOS_0),
        retain=retain,
    )


class Bridge:
    """High-level helper that mirrors the bridge daemon MQTT contract."""

    def __init__(
        self,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
        topic_prefix: str = MQTT_TOPIC_PREFIX,
        username: Optional[str] = MQTT_USER,
        password: Optional[str] = MQTT_PASS,
    ) -> None:
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix
        self.username = username
        self.password = password
        self._client: Optional[AiomqttClient] = None
        self._response_routes: Dict[
            str,
            List[Tuple[asyncio.Queue[InboundMessage], bool]],
        ] = {}
        self._correlation_routes: Dict[
            bytes,
            asyncio.Queue[InboundMessage],
        ] = {}
        self._reply_topic: Optional[str] = None
        self._listener_task: Optional[asyncio.Task[None]] = None
        self._digital_modes: Dict[int, int] = {}
        # Internal stack for managing the context manager
        self._exit_stack: Optional[Any] = None

    async def connect(self) -> None:
        if self._client is not None:
            await self.disconnect()

        # We manually enter the client context manager to keep connection open
        # until disconnect() is called.
        self._client = Client(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            logger=logging.getLogger("yunbridge.examples.bridge"),
        )
        await self._client.__aenter__()
        
        logger.info("Connected to MQTT broker at %s:%d", self.host, self.port)
        self._digital_modes.clear()
        self._response_routes.clear()
        self._correlation_routes.clear()
        self._reply_topic = (
            f"{self.topic_prefix}/client/{uuid.uuid4().hex}/reply"
        )
        try:
            await self._client.subscribe(
                self._reply_topic,
                qos=int(QOSLevel.QOS_0),
            )
            logger.debug("Subscribed to reply topic %s", self._reply_topic)
        except MQTTError:
            logger.warning(
                "Failed to subscribe to reply topic %s",
                self._reply_topic,
            )
        self._listener_task = asyncio.create_task(self._message_listener())

    async def disconnect(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._response_routes.clear()
                self._correlation_routes.clear()
                self._reply_topic = None
                self._client = None
                logger.info("Disconnected from MQTT broker.")

    def _ensure_client(self) -> AiomqttClient:
        client = self._client
        if client is None:
            raise ConnectionError(
                "MQTT client not connected. Call connect() first."
            )
        return client

    async def _message_listener(self) -> None:
        client = self._ensure_client()

        try:
            async with client.messages() as messages:
                async for raw_message in messages:
                    inbound = as_inbound_message(raw_message)
                    await self._handle_inbound_message(inbound)
        except asyncio.CancelledError:
            raise
        except MQTTError as exc:  # pragma: no cover - defensive guard
            logger.debug("MQTT listener stopped: %s", exc)
        except Exception:  # pragma: no cover - unexpected failure
            logger.exception("Unexpected error in MQTT listener")

    async def _handle_inbound_message(
        self, message: InboundMessage
    ) -> None:
        topic = message.topic_name
        if not topic:
            return

        logger.debug(
            "MQTT message observed topic=%s size=%d qos=%d",
            topic,
            len(message.payload),
            int(message.qos),
        )

        handled = False
        correlation = message.correlation_data
        if correlation is not None:
            queue = self._correlation_routes.pop(correlation, None)
            if queue is not None:
                self._safe_queue_put(queue, message, drop_oldest=False)
                handled = True

        for prefix, queues in list(self._response_routes.items()):
            if not topic.startswith(prefix):
                continue
            handled = True
            for queue, drop_oldest in list(queues):
                self._safe_queue_put(queue, message, drop_oldest=drop_oldest)

        if not handled:
            preview = message.payload[:128]
            text = preview.decode("utf-8", errors="ignore")
            logger.debug(
                "Received unhandled MQTT message: %s -> %s",
                topic,
                text,
            )

    def _safe_queue_put(
        self,
        queue: asyncio.Queue[InboundMessage],
        message: InboundMessage,
        *,
        drop_oldest: bool,
    ) -> None:
        try:
            queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            if not drop_oldest:
                logger.debug("Queue full; overwriting oldest entry")
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                if drop_oldest:
                    logger.debug("Queue empty despite full state; skipping")
                    return
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Failed to enqueue MQTT message for consumer")

    def _register_route(
        self,
        prefix: str,
        queue: asyncio.Queue[InboundMessage],
        *,
        drop_oldest: bool = False,
    ) -> None:
        routes = self._response_routes.setdefault(prefix, [])
        routes.append((queue, drop_oldest))

    def _unregister_route(
        self,
        prefix: str,
        queue: asyncio.Queue[InboundMessage],
    ) -> None:
        routes = self._response_routes.get(prefix)
        if not routes:
            return
        for entry in list(routes):
            if entry[0] is queue:
                routes.remove(entry)
                break
        if not routes:
            self._response_routes.pop(prefix, None)

    async def _publish_and_wait(
        self,
        pub_topic: str,
        pub_payload: bytes,
        *,
        resp_topic: Union[str, Sequence[str], Iterable[str]],
        timeout: float = 10,
    ) -> bytes:
        client = self._ensure_client()
        reply_topic = self._reply_topic
        if reply_topic is None:
            raise RuntimeError("Reply topic not initialised; call connect()")
        if isinstance(resp_topic, str):
            topics: tuple[str, ...] = (resp_topic,)
        else:
            topics = tuple(resp_topic)
        if not topics:
            raise ValueError("resp_topic must contain at least one topic")

        response_queue: asyncio.Queue[InboundMessage] = asyncio.Queue(
            maxsize=1
        )
        correlation = secrets.token_bytes(12)
        subscribed = False
        try:
            for topic in topics:
                self._register_route(topic, response_queue)
            self._correlation_routes[correlation] = response_queue

            try:
                await _subscribe_many(client, topics)
                subscribed = True
            except MQTTError:
                logger.debug(
                    "Subscription to response topics %s failed; "
                    "relying on reply topic",
                    topics,
                )

            message = PublishableMessage(
                topic_name=pub_topic,
                payload=pub_payload,
                qos=QOSLevel.QOS_0,
                retain=False,
            )
            message = message.with_response_topic(reply_topic)
            message = message.with_correlation_data(correlation)
            await client.publish(
                message.topic_name,
                message.payload,
                qos=int(message.qos),
                retain=message.retain,
                properties=message.build_properties(),
            )
            delivered = await asyncio.wait_for(
                response_queue.get(), timeout=timeout
            )
            return delivered.payload
        finally:
            self._correlation_routes.pop(correlation, None)
            for topic in topics:
                self._unregister_route(topic, response_queue)
            try:
                if subscribed:
                    await _unsubscribe_many(client, topics)
            except MQTTError:
                logger.debug("Ignoring MQTT unsubscribe error")

    async def digital_write(self, pin: int, value: int) -> None:
        if self._digital_modes.get(pin) != 1:
            await self.set_digital_mode(pin, 1)
        topic = f"{self.topic_prefix}/d/{pin}"
        await _publish_simple(self._ensure_client(), topic, str(value))
        logger.debug("digital_write(%d, %d) -> %s", pin, value, topic)

    async def digital_read(self, pin: int, timeout: float = 10) -> int:
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/d/{pin}/read",
            b"",
            resp_topic=(
                f"{self.topic_prefix}/d/{pin}/value",
                f"{self.topic_prefix}/d/value",
            ),
            timeout=timeout,
        )
        return int(response.decode("utf-8"))

    async def analog_read(self, pin: int, timeout: float = 10) -> int:
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/a/{pin}/read",
            b"",
            resp_topic=(
                f"{self.topic_prefix}/a/{pin}/value",
                f"{self.topic_prefix}/a/value",
            ),
            timeout=timeout,
        )
        return int(response.decode("utf-8"))

    async def set_digital_mode(
        self, pin: int, mode: Union[int, str]
    ) -> None:
        if isinstance(mode, str):
            normalized = mode.strip().lower()
            mode_map = {
                "input": 0,
                "output": 1,
                "input_pullup": 2,
                "pullup": 2,
            }
            if normalized not in mode_map:
                raise ValueError(f"Unknown digital mode '{mode}'")
            mode_value = mode_map[normalized]
        else:
            mode_value = int(mode)

        if mode_value not in (0, 1, 2):
            raise ValueError(f"Invalid digital mode value: {mode}")

        topic = f"{self.topic_prefix}/d/{pin}/mode"
        await _publish_simple(self._ensure_client(), topic, str(mode_value))
        self._digital_modes[pin] = mode_value
        logger.debug("set_digital_mode(%d, %d)", pin, mode_value)

    async def put(self, key: str, value: str, timeout: float = 10) -> None:
        await self._publish_and_wait(
            f"{self.topic_prefix}/datastore/put/{key}",
            value.encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/datastore/get/{key}",
            timeout=timeout,
        )
        logger.debug("datastore put('%s', '%s')", key, value)

    async def get(self, key: str, timeout: float = 10) -> str:
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/datastore/get/{key}/request",
            b"",
            resp_topic=f"{self.topic_prefix}/datastore/get/{key}",
            timeout=timeout,
        )
        return response.decode("utf-8")

    async def get_free_memory(self, timeout: float = 10) -> int:
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/system/free_memory/get",
            b"",
            resp_topic=f"{self.topic_prefix}/system/free_memory/value",
            timeout=timeout,
        )
        return int(response.decode("utf-8"))

    async def run_sketch_command(
        self, command_parts: List[str], timeout: float = 10
    ) -> bytes:
        command_str = _format_shell_command(command_parts)
        logger.warning(
            "run_sketch_command falls back to a synchronous shell "
            "command via MQTT."
        )
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/run",
            command_str.encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/sh/response",
            timeout=timeout,
        )
        return response

    async def run_shell_command_async(
        self, command_parts: List[str], timeout: float = 10
    ) -> int:
        command_str = _format_shell_command(command_parts)
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/run_async",
            command_str.encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/sh/run_async/response",
            timeout=timeout,
        )
        return int(response.decode("utf-8"))

    async def poll_shell_process(
        self,
        pid: int,
        *,
        timeout: float = 10,
    ) -> ShellPollResponse:
        if pid <= 0:
            raise ValueError("pid must be a positive integer")
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/poll/{pid}",
            b"",
            resp_topic=f"{self.topic_prefix}/sh/poll/{pid}/response",
            timeout=timeout,
        )
        try:
            payload = json.loads(response.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Malformed process poll response") from exc
        if not isinstance(payload, dict):
            raise ValueError("Process poll response must be an object")
        payload_dict = cast(Dict[str, Any], payload)
        return cast(ShellPollResponse, payload_dict)

    async def console_write(self, message: str) -> None:
        topic = f"{self.topic_prefix}/console/in"
        await _publish_simple(self._ensure_client(), topic, message)
        logger.debug("console_write('%s')", message)

    async def console_read_async(self) -> Optional[str]:
        topic = f"{self.topic_prefix}/console/out"
        client = self._ensure_client()
        queue: Optional[asyncio.Queue[InboundMessage]] = None
        routes = self._response_routes.get(topic)
        if routes:
            queue = routes[0][0]

        if queue is None:
            queue = asyncio.Queue(maxsize=100)
            self._register_route(topic, queue, drop_oldest=True)
            await client.subscribe(topic, qos=int(QOSLevel.QOS_0))
            logger.debug("Subscribed to console output topic: %s", topic)

        try:
            message = await asyncio.wait_for(queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Error reading from console queue")
            return None

        return message.payload.decode("utf-8", errors="ignore")

    async def mailbox_read(self, timeout: float = 5.0) -> Optional[bytes]:
        incoming_topic = f"{self.topic_prefix}/mailbox/incoming"
        try:
            payload = await self._publish_and_wait(
                f"{self.topic_prefix}/mailbox/read",
                b"",
                resp_topic=incoming_topic,
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return None
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Error waiting for mailbox message")
            return None

        if not payload:
            return None

        return payload

    async def file_write(
        self, filename: str, content: Union[str, bytes]
    ) -> None:
        payload = (
            content.encode("utf-8") if isinstance(content, str) else content
        )
        topic = f"{self.topic_prefix}/file/write/{filename}"
        await _publish_simple(self._ensure_client(), topic, payload)
        logger.debug("file_write('%s', %d bytes)", filename, len(payload))

    async def file_read(self, filename: str, timeout: float = 10) -> bytes:
        return await self._publish_and_wait(
            f"{self.topic_prefix}/file/read/{filename}",
            b"",
            resp_topic=f"{self.topic_prefix}/file/read/response/{filename}",
            timeout=timeout,
        )

    async def file_remove(self, filename: str) -> None:
        topic = f"{self.topic_prefix}/file/remove/{filename}"
        await _publish_simple(self._ensure_client(), topic, b"")
        logger.debug("file_remove('%s')", filename)

    async def mailbox_write(self, message: Union[str, bytes]) -> None:
        payload = (
            message.encode("utf-8") if isinstance(message, str) else message
        )
        topic = f"{self.topic_prefix}/mailbox/write"
        await _publish_simple(self._ensure_client(), topic, payload)
        logger.debug("mailbox_write(%d bytes)", len(payload))
