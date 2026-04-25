"""Minimalistic Async MQTT Client for MCU Bridge. [Zero-Boilerplate]"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shlex
import ssl
import uuid
from collections.abc import Iterable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TypedDict, cast, Any

import msgspec
from aiomqtt import Client, MqttError, ProtocolVersion
from aiomqtt.message import Message

from .definitions import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    QOSLevel,
    QueuedPublish,
    SpiBitOrder,
    SpiMode,
    build_bridge_args,
    build_mqtt_properties,
)
from .env import dump_client_env, read_uci_general
from .protocol import Command, Topic, Status
from .spi import SpiDevice

__all__ = [
    "Bridge",
    "SpiBitOrder",
    "SpiMode",
    "SpiDevice",
    "build_bridge_args",
    "dump_client_env",
    "MqttError",
    "QOSLevel",
    "Command",
    "Topic",
    "Status",
]

logger = logging.getLogger(__name__)
_UCI_GENERAL = read_uci_general()

MQTT_HOST = os.environ.get("MCUBRIDGE_MQTT_HOST") or _UCI_GENERAL.get(
    "mqtt_host", DEFAULT_MQTT_HOST
)
MQTT_PORT = int(
    os.environ.get("MCUBRIDGE_MQTT_PORT")
    or _UCI_GENERAL.get("mqtt_port", str(DEFAULT_MQTT_PORT))
)
MQTT_TOPIC_PREFIX = os.environ.get("MCUBRIDGE_MQTT_TOPIC") or _UCI_GENERAL.get(
    "mqtt_topic", DEFAULT_MQTT_TOPIC
)
MQTT_USER = (
    os.environ.get("MCUBRIDGE_MQTT_USER") or _UCI_GENERAL.get("mqtt_user") or None
)
MQTT_PASS = (
    os.environ.get("MCUBRIDGE_MQTT_PASS") or _UCI_GENERAL.get("mqtt_pass") or None
)
MQTT_TLS_INSECURE = (
    os.environ.get("MCUBRIDGE_MQTT_TLS_INSECURE")
    or _UCI_GENERAL.get("mqtt_tls_insecure")
    or "0"
)


class ShellPollResponse(TypedDict, total=False):
    status_byte: int
    exit_code: int
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool


def _default_tls_context() -> ssl.SSLContext | None:
    mqtt_tls = _UCI_GENERAL.get("mqtt_tls", "0")
    if str(mqtt_tls).strip() not in {"1", "true", "yes", "on"}:
        return None
    try:
        cafile = (_UCI_GENERAL.get("mqtt_cafile") or "").strip()
        if not cafile and Path("/etc/ssl/certs/ca-certificates.crt").exists():
            cafile = "/etc/ssl/certs/ca-certificates.crt"

        ctx = (
            ssl.create_default_context(cafile=cafile)
            if cafile
            else ssl.create_default_context()
        )
        if str(MQTT_TLS_INSECURE).strip() in {"1", "true", "yes", "on"}:
            ctx.check_hostname = False
        return ctx
    except (ssl.SSLError, OSError, ValueError):
        return None


class Bridge:
    """Zero-Boilerplate high-level client for MCU Bridge."""

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
        Topic.PREFIX = topic_prefix
        self.username = username
        self.password = password
        self.tls_context = tls_context or _default_tls_context()

        self._client: Client | None = None
        self._correlation_routes: dict[bytes, asyncio.Queue[Message]] = {}
        self._reply_topic: str | None = None
        self._console_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._listener_task: asyncio.Task[None] | None = None
        self._exit_stack = AsyncExitStack()

    async def connect(self) -> None:
        if self._client:
            await self.disconnect()
        self._client = Client(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            logger=logging.getLogger("mcubridge.client"),
            protocol=ProtocolVersion.V5,
            tls_context=self.tls_context,
        )
        await self._exit_stack.enter_async_context(self._client)
        self._reply_topic = f"{self.topic_prefix}/client/{uuid.uuid4().hex}/reply"
        await self._client.subscribe(self._reply_topic, qos=0)
        self._console_topic = str(Topic.build(Topic.CONSOLE, "out"))
        await self._client.subscribe(self._console_topic, qos=0)
        self._listener_task = asyncio.create_task(self._message_listener())
        logger.info("Connected to %s:%d. Reply topic: %s", self.host, self.port, self._reply_topic)

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        await self._exit_stack.aclose()
        self._client = None
        logger.info("Disconnected.")

    async def _message_listener(self) -> None:
        if not self._client:
            return
        try:
            async for message in self._client.messages:
                props = message.properties
                correlation = getattr(props, "CorrelationData", None) if props else None
                if correlation and (queue := self._correlation_routes.pop(correlation, None)):
                    queue.put_nowait(message)
                elif Topic.matches(self._console_topic, str(message.topic)):
                    self._console_queue.put_nowait(bytes(message.payload) if message.payload else b"")
        except MqttError:
            pass

    async def _publish(self, topic: str, payload: Any, qos: int = 0) -> None:
        """Atomic publish helper (Zero-Wrapper)."""
        if not self._client:
            raise ConnectionError("Not connected")
        payload_bytes = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        await self._client.publish(topic, payload_bytes, qos=qos)

    async def _request(
        self,
        topic: str,
        payload: Any = b"",
        *,
        resp_topic: str | Iterable[str] | None = None,
        timeout: float = 15,
    ) -> bytes:
        """Atomic request-response helper (Zero-Wrapper)."""
        if not self._client:
            raise ConnectionError("Not connected")

        correlation = secrets.token_bytes(12)
        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)
        self._correlation_routes[correlation] = queue

        resp_topics = [resp_topic] if isinstance(resp_topic, str) else list(resp_topic or [])
        for t in resp_topics:
            await self._client.subscribe(t)

        try:
            payload_bytes = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
            msg = QueuedPublish(
                topic_name=topic,
                payload=payload_bytes,
                response_topic=self._reply_topic,
                correlation_data=correlation,
            )
            await self._client.publish(msg.topic_name, msg.payload, properties=build_mqtt_properties(msg))
            delivered = await asyncio.wait_for(queue.get(), timeout=timeout)
            return bytes(delivered.payload) if delivered.payload else b""
        finally:
            self._correlation_routes.pop(correlation, None)
            for t in resp_topics:
                await self._client.unsubscribe(t)

    # --- Convenience methods (refactored to Zero-Boilerplate) ---

    async def console_write(self, data: str | bytes) -> None:
        await self._publish(Topic.build(Topic.CONSOLE, "in"), data)

    async def console_read_async(self) -> str | None:
        try:
            payload = await asyncio.wait_for(self._console_queue.get(), timeout=0.1)
            return payload.decode("utf-8", errors="replace")
        except (asyncio.TimeoutError, TimeoutError):
            return None

    async def digital_write(self, pin: int, value: int) -> None:
        await self._publish(Topic.build(Topic.DIGITAL, pin), value)

    async def digital_read(self, pin: int, timeout: float = 15) -> int:
        res = await self._request(Topic.build(Topic.DIGITAL, pin, "read"), b"", 
                                 resp_topic=Topic.build(Topic.DIGITAL, pin, "value"), timeout=timeout)
        return int(res.decode())

    async def analog_read(self, pin: int, timeout: float = 15) -> int:
        res = await self._request(Topic.build(Topic.ANALOG, pin, "read"), b"", 
                                 resp_topic=Topic.build(Topic.ANALOG, pin, "value"), timeout=timeout)
        return int(res.decode())

    async def analog_write(self, pin: int, value: int) -> None:
        await self._publish(Topic.build(Topic.ANALOG, pin), value)

    async def set_digital_mode(self, pin: int, mode: int) -> None:
        await self._publish(Topic.build(Topic.DIGITAL, pin, "mode"), mode)

    async def put(self, key: str, value: str, timeout: float = 15) -> None:
        await self._request(Topic.build(Topic.DATASTORE, "put", key), value, 
                           resp_topic=Topic.build(Topic.DATASTORE, "get", key), timeout=timeout)

    async def get(self, key: str, timeout: float = 15) -> str:
        res = await self._request(Topic.build(Topic.DATASTORE, "get", key, "request"), b"", 
                                 resp_topic=Topic.build(Topic.DATASTORE, "get", key), timeout=timeout)
        return res.decode()

    async def get_free_memory(self, timeout: float = 15) -> int:
        res = await self._request(Topic.build(Topic.SYSTEM, "free_memory", "get"), b"", 
                                 resp_topic=Topic.build(Topic.SYSTEM, "free_memory", "value"), timeout=timeout)
        return int(res.decode())

    async def run_shell_command_async(self, parts: list[str], timeout: float = 15) -> int:
        res = await self._request(Topic.build(Topic.SHELL, "run_async"), shlex.join(parts), 
                                 resp_topic=Topic.build(Topic.SHELL, "run_async", "response"), timeout=timeout)
        return int(res.decode())

    async def poll_shell_process(self, pid: int, timeout: float = 15) -> ShellPollResponse:
        res = await self._request(Topic.build(Topic.SHELL, "poll", pid), b"", 
                                 resp_topic=Topic.build(Topic.SHELL, "poll", pid, "response"), timeout=timeout)
        return cast(ShellPollResponse, msgspec.msgpack.decode(res))

    async def spi_begin(self) -> None:
        await self._publish(Topic.build(Topic.SPI, "begin"), b"")

    async def spi_end(self) -> None:
        await self._publish(Topic.build(Topic.SPI, "end"), b"")

    async def spi_config(self, frequency: int = 4000000, bit_order: int = 1, data_mode: int = 0) -> None:
        config = {"frequency": frequency, "bit_order": bit_order, "data_mode": data_mode}
        await self._publish(Topic.build(Topic.SPI, "config"), msgspec.json.encode(config))

    async def spi_transfer(self, data: bytes, timeout: float = 15) -> bytes:
        return await self._request(Topic.build(Topic.SPI, "transfer"), data, 
                                 resp_topic=Topic.build(Topic.SPI, "transfer", "resp"), timeout=timeout)

    async def file_write(self, filename: str, content: str | bytes) -> None:
        await self._publish(Topic.build(Topic.FILE, "write", filename.lstrip("/")), content)

    async def file_read(self, filename: str, timeout: float = 15) -> bytes:
        return await self._request(Topic.build(Topic.FILE, "read", filename.lstrip("/")), b"", 
                                 resp_topic=Topic.build(Topic.FILE, "read", "response", filename.lstrip("/")), timeout=timeout)

    async def file_remove(self, filename: str) -> None:
        await self._publish(Topic.build(Topic.FILE, "remove", filename.lstrip("/")), b"")

    async def mailbox_write(self, message: str | bytes) -> None:
        await self._publish(Topic.build(Topic.MAILBOX, "write"), message)

    async def mailbox_read(self, timeout: float = 5.0) -> bytes | None:
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            return await self._request(Topic.build(Topic.MAILBOX, "read"), b"", 
                                      resp_topic=Topic.build(Topic.MAILBOX, "incoming"), timeout=timeout)
        return None
