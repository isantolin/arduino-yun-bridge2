"""Async MQTT helpers for MCU Bridge example scripts."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import ssl
import uuid
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, cast

import msgspec
from aiomqtt import Client, Message, MqttError, ProtocolVersion

from .definitions import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    QOSLevel,
    build_bridge_args,
    build_mqtt_properties,
)
from .env import dump_client_env, read_uci_general
from .protocol import Command

__all__ = [
    "Bridge",
    "build_bridge_args",
    "dump_client_env",
    "MqttError",
    "QOSLevel",
    "Command",
]


_UCI_GENERAL = read_uci_general()

MQTT_HOST = os.environ.get("MCUBRIDGE_MQTT_HOST") or _UCI_GENERAL.get("mqtt_host", DEFAULT_MQTT_HOST)
MQTT_PORT = int(os.environ.get("MCUBRIDGE_MQTT_PORT") or _UCI_GENERAL.get("mqtt_port", str(DEFAULT_MQTT_PORT)))
MQTT_TOPIC_PREFIX = os.environ.get("MCUBRIDGE_MQTT_TOPIC") or _UCI_GENERAL.get("mqtt_topic", DEFAULT_MQTT_TOPIC)
MQTT_USER = os.environ.get("MCUBRIDGE_MQTT_USER") or _UCI_GENERAL.get("mqtt_user") or None
MQTT_PASS = os.environ.get("MCUBRIDGE_MQTT_PASS") or _UCI_GENERAL.get("mqtt_pass") or None
MQTT_TLS_INSECURE = os.environ.get("MCUBRIDGE_MQTT_TLS_INSECURE") or _UCI_GENERAL.get("mqtt_tls_insecure") or "0"


def _default_tls_context() -> ssl.SSLContext | None:
    mqtt_tls = _UCI_GENERAL.get("mqtt_tls", "0")
    if str(mqtt_tls).strip() not in {"1", "true", "yes", "on"}:
        return None

    ctx = ssl.create_default_context()
    if str(MQTT_TLS_INSECURE).strip() in {"1", "true", "yes", "on"}:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    mqtt_certfile = _UCI_GENERAL.get("mqtt_certfile")
    mqtt_keyfile = _UCI_GENERAL.get("mqtt_keyfile")
    if mqtt_certfile and mqtt_keyfile:
        ctx.load_cert_chain(certfile=mqtt_certfile, keyfile=mqtt_keyfile)

    return ctx


logger = logging.getLogger("mcubridge.client")

if TYPE_CHECKING:
    from typing_extensions import TypedDict

    class ShellPollResponse(TypedDict):
        pid: int
        running: bool
        exit_code: int
        stdout: str
        stderr: str


PayloadType = bytes | str | bytearray | None


class Bridge:
    """High-level helper that mirrors the bridge daemon MQTT contract."""

    def __init__(
        self,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
        topic_prefix: str = MQTT_TOPIC_PREFIX,
        username: str | None = MQTT_USER,
        password: str | None = MQTT_PASS,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix
        self.username = username
        self.password = password
        self.tls_context = tls_context if tls_context is not None else _default_tls_context()
        self._client: Client | None = None
        self._response_routes: dict[
            str, list[tuple[asyncio.Queue[Message], bool]]
        ] = {}  # topic -> list[(queue, drop_oldest)]
        self._reply_topic: str | None = None
        self._exit_stack = AsyncExitStack()
        self._digital_modes: dict[int, int] = {}  # pin -> mode

    async def connect(self) -> None:
        if self._client is not None:
            return

        self._reply_topic = f"mcubridge/client/{uuid.uuid4()}"
        self._client = Client(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            protocol=ProtocolVersion.V5,
            tls_context=self.tls_context,
        )
        await self._exit_stack.enter_async_context(self._client)

        logger.info("Connected to MQTT broker at %s:%d", self.host, self.port)
        self._digital_modes.clear()

        # Start background listener for response routes
        asyncio.create_task(self._listen_for_responses())

    async def disconnect(self) -> None:
        await self._exit_stack.aclose()
        self._client = None
        self._reply_topic = None

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

    async def run_sketch_command(self, command_parts: list[str], timeout: float = 10) -> bytes:
        command_str = _format_shell_command(command_parts)
        logger.warning("run_sketch_command falls back to a synchronous shell command via MQTT.")
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/run",
            command_str.encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/sh/response",
            timeout=timeout,
        )
        return response

    async def run_shell_command_async(self, command_parts: list[str], timeout: float = 10) -> int:
        command_str = _format_shell_command(command_parts)
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/run_async",
            command_str.encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/sh/run_async/response",
            timeout=timeout,
        )
        text = response.decode("utf-8")
        if text.startswith("error:"):
            raise RuntimeError(f"Shell command rejected: {text}")
        return int(text)

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
            payload = msgspec.json.decode(response)
        except msgspec.DecodeError as exc:
            raise ValueError("Malformed process poll response") from exc
        if not isinstance(payload, dict):
            raise ValueError("Process poll response must be an object")
        payload_dict = cast(dict[str, Any], payload)
        return cast("ShellPollResponse", payload_dict)

    async def console_write(self, message: str) -> None:
        topic = f"{self.topic_prefix}/console/in"
        await self._publish_simple(topic, message)
        logger.debug("console_write('%s')", message)

    async def console_read_async(self) -> str | None:
        topic = f"{self.topic_prefix}/console/out"
        client = self._ensure_client()
        queue: asyncio.Queue[Message] | None = None
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
        except (TimeoutError, asyncio.TimeoutError):
            return None
        except (asyncio.CancelledError, OSError, RuntimeError) as exc:
            logger.error("Error reading from console queue: %s", exc)
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
        except (TimeoutError, asyncio.TimeoutError):
            return None
        except (asyncio.CancelledError, MqttError, OSError) as exc:
            logger.error("Error waiting for mailbox message: %s", exc)
            return None

        if not payload:
            return None

        return payload

    async def file_write(self, filename: str, content: str | bytes) -> None:
        fn = filename.lstrip("/")
        topic = f"{self.topic_prefix}/file/write/{fn}"
        await self._publish_simple(topic, content)
        logger.debug("file_write('%s', %d bytes)", filename, len(content))

    async def file_read(self, filename: str, timeout: float = 10) -> bytes:
        fn = filename.lstrip("/")
        return await self._publish_and_wait(
            f"{self.topic_prefix}/file/read/{fn}",
            b"",
            resp_topic=f"{self.topic_prefix}/file/read/response/{fn}",
            timeout=timeout,
        )

    async def file_remove(self, filename: str) -> None:
        fn = filename.lstrip("/")
        topic = f"{self.topic_prefix}/file/remove/{fn}"
        await self._publish_simple(topic, b"")
        logger.debug("file_remove('%s')", filename)

    async def mailbox_write(self, message: str | bytes) -> None:
        topic = f"{self.topic_prefix}/mailbox/write"
        await self._publish_simple(topic, message)
        logger.debug("mailbox_write(%d bytes)", len(message))

    async def _publish_simple(self, topic: str, payload: PayloadType) -> None:
        client = self._ensure_client()
        await client.publish(topic, _payload_bytes(payload), qos=1)

    async def _publish_and_wait(
        self,
        pub_topic: str,
        pub_payload: PayloadType,
        *,
        resp_topic: str | Sequence[str],
        timeout: float = 10,
    ) -> bytes:
        client = self._ensure_client()
        reply_topic = self._reply_topic
        if reply_topic is None:
            raise RuntimeError("Reply topic not initialised; call connect()")

        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)
        resp_topics = [resp_topic] if isinstance(resp_topic, str) else list(resp_topic)

        for rt in resp_topics:
            self._register_route(rt, queue)
            await client.subscribe(rt, qos=1)

        try:
            props = build_mqtt_properties(response_topic=reply_topic)
            await client.publish(
                pub_topic,
                _payload_bytes(pub_payload),
                qos=1,
                properties=props,
            )
            message = await asyncio.wait_for(queue.get(), timeout=timeout)
            return _payload_bytes(message.payload)
        finally:
            for rt in resp_topics:
                self._unregister_route(rt, queue)

    def _ensure_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Not connected to MQTT broker; call connect() first")
        return self._client

    def _register_route(
        self, topic: str, queue: asyncio.Queue[Message], drop_oldest: bool = False
    ) -> None:
        if topic not in self._response_routes:
            self._response_routes[topic] = []
        self._response_routes[topic].append((queue, drop_oldest))

    def _unregister_route(self, topic: str, queue: asyncio.Queue[Message]) -> None:
        if topic in self._response_routes:
            self._response_routes[topic] = [r for r in self._response_routes[topic] if r[0] is not queue]
            if not self._response_routes[topic]:
                del self._response_routes[topic]

    async def _listen_for_responses(self) -> None:
        client = self._ensure_client()
        async for message in client.messages:
            topic = str(message.topic)
            routes = self._response_routes.get(topic)
            if routes:
                for queue, drop_oldest in routes:
                    if drop_oldest and queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(message)
                    except asyncio.QueueFull:
                        pass


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


def _format_shell_command(parts: list[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)
