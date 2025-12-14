"""Async MQTT helpers for Yun Bridge example scripts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shlex
import uuid
from contextlib import AsyncExitStack
from typing import Any, TypedDict, cast
from collections.abc import Iterable, Sequence

from aiomqtt import Client as MqttClient, MqttError, ProtocolVersion
from aiomqtt.message import Message as MQTTMessage
from aiomqtt.types import PayloadType
from yunbridge.mqtt.messages import QOSLevel, QueuedPublish
from yunbridge.common import build_mqtt_properties
from yunbridge.const import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
)
from .env import dump_client_env

__all__ = [
    "Bridge",
    "dump_client_env",
    "MqttError",
    "QOSLevel",
]

MQTT_HOST = os.environ.get("YUN_BROKER_IP", DEFAULT_MQTT_HOST)
MQTT_PORT = int(os.environ.get("YUN_BROKER_PORT", DEFAULT_MQTT_PORT))
MQTT_TOPIC_PREFIX = DEFAULT_MQTT_TOPIC
MQTT_USER = os.environ.get("YUN_BROKER_USER")
MQTT_PASS = os.environ.get("YUN_BROKER_PASS")

logger = logging.getLogger(__name__)


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
    return shlex.join(parts)


def _payload_bytes(payload: PayloadType) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if payload is None:
        return b""
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return str(payload).encode("utf-8")


class Bridge:
    """High-level helper that mirrors the bridge daemon MQTT contract."""

    def __init__(
        self,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
        topic_prefix: str = MQTT_TOPIC_PREFIX,
        username: str | None = MQTT_USER,
        password: str | None = MQTT_PASS,
    ) -> None:
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix
        self.username = username
        self.password = password
        self._client: MqttClient | None = None
        self._response_routes: dict[
            str,
            list[tuple[asyncio.Queue[MQTTMessage], bool]],
        ] = {}
        self._correlation_routes: dict[
            bytes,
            asyncio.Queue[MQTTMessage],
        ] = {}
        self._reply_topic: str | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._digital_modes: dict[int, int] = {}
        self._exit_stack = AsyncExitStack()

    async def connect(self) -> None:
        if self._client is not None:
            await self.disconnect()

        self._client = MqttClient(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            logger=logging.getLogger("yunbridge.examples.bridge"),
            protocol=ProtocolVersion.V5,
        )
        await self._exit_stack.enter_async_context(self._client)

        logger.info("Connected to MQTT broker at %s:%d", self.host, self.port)
        self._digital_modes.clear()
        self._response_routes.clear()
        self._correlation_routes.clear()
        self._reply_topic = f"{self.topic_prefix}/client/{uuid.uuid4().hex}/reply"
        try:
            await self._client.subscribe(
                self._reply_topic,
                qos=int(QOSLevel.QOS_0),
            )
            logger.debug("Subscribed to reply topic %s", self._reply_topic)
        except MqttError:
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

        await self._exit_stack.aclose()
        self._response_routes.clear()
        self._correlation_routes.clear()
        self._reply_topic = None
        self._client = None
        logger.info("Disconnected from MQTT broker.")

    def _ensure_client(self) -> MqttClient:
        client = self._client
        if client is None:
            raise ConnectionError("MQTT client not connected. Call connect() first.")
        return client

    async def _message_listener(self) -> None:
        client = self._ensure_client()

        try:
            async for message in client.messages:
                await self._handle_inbound_message(message)
        except asyncio.CancelledError:
            raise
        except MqttError as exc:
            logger.debug("MQTT listener stopped: %s", exc)
        except Exception:
            logger.exception("Unexpected error in MQTT listener")

    async def _handle_inbound_message(self, message: MQTTMessage) -> None:
        topic = str(message.topic)
        if not topic:
            return

        payload = _payload_bytes(message.payload)

        logger.debug(
            "MQTT message observed topic=%s size=%d qos=%d",
            topic,
            len(payload),
            int(message.qos),
        )

        handled = False
        props = getattr(message, "properties", None)
        correlation = getattr(props, "CorrelationData", None) if props else None
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
            preview = payload[:128]
            text = preview.decode("utf-8", errors="ignore")
            logger.debug(
                "Received unhandled MQTT message: %s -> %s",
                topic,
                text,
            )

    def _safe_queue_put(
        self,
        queue: asyncio.Queue[MQTTMessage],
        message: MQTTMessage,
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
        queue: asyncio.Queue[MQTTMessage],
        *,
        drop_oldest: bool = False,
    ) -> None:
        routes = self._response_routes.setdefault(prefix, [])
        routes.append((queue, drop_oldest))

    def _unregister_route(
        self,
        prefix: str,
        queue: asyncio.Queue[MQTTMessage],
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
        resp_topic: str | Sequence[str] | Iterable[str],
        timeout: float = 10,
    ) -> bytes:
        client = self._ensure_client()
        reply_topic = self._reply_topic
        if reply_topic is None:
            raise RuntimeError("Reply topic not initialised; call connect()")

        topics: tuple[str, ...]
        if isinstance(resp_topic, str):
            topics = (resp_topic,)
        else:
            topics = tuple(resp_topic)
        if not topics:
            raise ValueError("resp_topic must contain at least one topic")

        response_queue: asyncio.Queue[MQTTMessage] = asyncio.Queue(maxsize=1)
        correlation = secrets.token_bytes(12)
        subscribed = False
        try:
            for topic in topics:
                self._register_route(topic, response_queue)
            self._correlation_routes[correlation] = response_queue

            try:
                # aiomqtt subscribe accepts simple topic string or list
                for t in topics:
                    await client.subscribe(t, qos=0)
                subscribed = True
            except MqttError:
                logger.debug(
                    "Subscription to response topics %s failed; "
                    "relying on reply topic",
                    topics,
                )

            # Construct message envelope to use our shared builder logic
            message = QueuedPublish(
                topic_name=pub_topic,
                payload=pub_payload,
                qos=int(QOSLevel.QOS_0),
                retain=False,
                response_topic=reply_topic,
                correlation_data=correlation,
            )

            props = build_mqtt_properties(message)

            await client.publish(
                message.topic_name,
                message.payload,
                qos=int(message.qos),
                retain=message.retain,
                properties=props,
            )

            delivered = await asyncio.wait_for(response_queue.get(), timeout=timeout)
            return _payload_bytes(delivered.payload)
        finally:
            self._correlation_routes.pop(correlation, None)
            for topic in topics:
                self._unregister_route(topic, response_queue)
            try:
                if subscribed:
                    for t in topics:
                        await client.unsubscribe(t)
            except MqttError:
                logger.debug("Ignoring MQTT unsubscribe error")

    async def _publish_simple(
        self,
        topic: str,
        payload: str | bytes,
        retain: bool = False,
    ) -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        await self._ensure_client().publish(
            topic,
            data,
            qos=0,
            retain=retain,
        )

    async def digital_write(self, pin: int, value: int) -> None:
        if self._digital_modes.get(pin) != 1:
            await self.set_digital_mode(pin, 1)
        topic = f"{self.topic_prefix}/d/{pin}"
        await self._publish_simple(topic, str(value))
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

    async def set_digital_mode(self, pin: int, mode: int | str) -> None:
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
        await self._publish_simple(topic, str(mode_value))
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
        self, command_parts: list[str], timeout: float = 10
    ) -> bytes:
        command_str = _format_shell_command(command_parts)
        logger.warning(
            "run_sketch_command falls back to a synchronous shell " "command via MQTT."
        )
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/run",
            command_str.encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/sh/response",
            timeout=timeout,
        )
        return response

    async def run_shell_command_async(
        self, command_parts: list[str], timeout: float = 10
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
        payload_dict = cast(dict[str, Any], payload)
        return cast(ShellPollResponse, payload_dict)

    async def console_write(self, message: str) -> None:
        topic = f"{self.topic_prefix}/console/in"
        await self._publish_simple(topic, message)
        logger.debug("console_write('%s')", message)

    async def console_read_async(self) -> str | None:
        topic = f"{self.topic_prefix}/console/out"
        client = self._ensure_client()
        queue: asyncio.Queue[MQTTMessage] | None = None
        routes = self._response_routes.get(topic)
        if routes:
            queue = routes[0][0]

        if queue is None:
            queue = asyncio.Queue(maxsize=100)
            self._register_route(topic, queue, drop_oldest=True)
            await client.subscribe(topic, qos=0)
            logger.debug("Subscribed to console output topic: %s", topic)

        try:
            message = await asyncio.wait_for(queue.get(), timeout=0.1)
        except TimeoutError:
            return None
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Error reading from console queue")
            return None

        payload = _payload_bytes(message.payload)
        return payload.decode("utf-8", errors="ignore")

    async def mailbox_read(self, timeout: float = 5.0) -> bytes | None:
        incoming_topic = f"{self.topic_prefix}/mailbox/incoming"
        try:
            payload = await self._publish_and_wait(
                f"{self.topic_prefix}/mailbox/read",
                b"",
                resp_topic=incoming_topic,
                timeout=timeout,
            )
        except TimeoutError:
            return None
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Error waiting for mailbox message")
            return None

        if not payload:
            return None

        return payload

    async def file_write(self, filename: str, content: str | bytes) -> None:
        topic = f"{self.topic_prefix}/file/write/{filename}"
        await self._publish_simple(topic, content)
        logger.debug("file_write('%s', %d bytes)", filename, len(content))

    async def file_read(self, filename: str, timeout: float = 10) -> bytes:
        return await self._publish_and_wait(
            f"{self.topic_prefix}/file/read/{filename}",
            b"",
            resp_topic=f"{self.topic_prefix}/file/read/response/{filename}",
            timeout=timeout,
        )

    async def file_remove(self, filename: str) -> None:
        topic = f"{self.topic_prefix}/file/remove/{filename}"
        await self._publish_simple(topic, b"")
        logger.debug("file_remove('%s')", filename)

    async def mailbox_write(self, message: str | bytes) -> None:
        topic = f"{self.topic_prefix}/mailbox/write"
        await self._publish_simple(topic, message)
        logger.debug("mailbox_write(%d bytes)", len(message))
