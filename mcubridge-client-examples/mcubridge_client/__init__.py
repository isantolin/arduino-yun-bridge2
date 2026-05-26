"""Minimalistic Async MQTT Client for MCU Bridge."""

from __future__ import annotations
from . import mcubridge_pb2 as pb

import asyncio
import contextlib
import logging
import os
import secrets
import shlex
import ssl
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TypedDict

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
from .protocol import (
    Command,
    Topic,
)
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
]

logger = logging.getLogger(__name__)
PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
_UCI_GENERAL = read_uci_general()

MQTT_HOST = os.environ.get("MCUBRIDGE_MQTT_HOST") or _UCI_GENERAL.get("mqtt_host", DEFAULT_MQTT_HOST)
MQTT_PORT = int(os.environ.get("MCUBRIDGE_MQTT_PORT") or _UCI_GENERAL.get("mqtt_port", str(DEFAULT_MQTT_PORT)))
MQTT_TOPIC_PREFIX = os.environ.get("MCUBRIDGE_MQTT_TOPIC") or _UCI_GENERAL.get("mqtt_topic", DEFAULT_MQTT_TOPIC)
MQTT_USER = os.environ.get("MCUBRIDGE_MQTT_USER") or _UCI_GENERAL.get("mqtt_user") or None
MQTT_PASS = os.environ.get("MCUBRIDGE_MQTT_PASS") or _UCI_GENERAL.get("mqtt_pass") or None
MQTT_TLS_INSECURE = os.environ.get("MCUBRIDGE_MQTT_TLS_INSECURE") or _UCI_GENERAL.get("mqtt_tls_insecure") or "0"


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

        ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        if str(MQTT_TLS_INSECURE).strip() in {"1", "true", "yes", "on"}:
            ctx.check_hostname = False
        return ctx
    except (ssl.SSLError, OSError, ValueError):
        return None


class Bridge:
    """Zero-Boilerplate high-level client for MCU Bridge (SIL-2)."""

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
                elif Topic.matches(self._console_topic, message.topic.value):
                    self._console_queue.put_nowait(bytes(message.payload) if message.payload else b"")
        except MqttError:
            pass

    async def _publish_and_wait(
        self,
        topic: str,
        payload: bytes | str,
        *,
        resp_topic: str | None = None,
        timeout: float = 15,
        content_type: str | None = None,
    ) -> bytes:
        if not self._client:
            raise ConnectionError("Not connected")

        correlation = secrets.token_bytes(12)
        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)
        self._correlation_routes[correlation] = queue

        if resp_topic:
            await self._client.subscribe(resp_topic)

        try:
            msg = QueuedPublish(
                topic_name=topic,
                payload=payload.encode() if isinstance(payload, str) else payload,
                response_topic=self._reply_topic,
                correlation_data=correlation,
                content_type=content_type,
            )
            await self._client.publish(msg.topic_name, msg.payload, properties=build_mqtt_properties(msg))
            delivered = await asyncio.wait_for(queue.get(), timeout=timeout)
            return bytes(delivered.payload)
        finally:
            self._correlation_routes.pop(correlation, None)
            if resp_topic:
                await self._client.unsubscribe(resp_topic)

    # --- Declarative API (Eradicates manual methods) ---

    async def console_write(self, data: str | bytes) -> None:
        payload = data if isinstance(data, bytes) else data.encode()
        await self._client.publish(Topic.build(Topic.CONSOLE, "in"), payload) if self._client else None

    async def console_read_async(self) -> str | None:
        try:
            payload = await asyncio.wait_for(self._console_queue.get(), timeout=0.1)
            return payload.decode("utf-8", errors="replace")
        except TimeoutError:
            return None

    async def digital_write(self, pin: int, value: int) -> None:
        await self._client.publish(Topic.build(Topic.DIGITAL, pin), str(value)) if self._client else None

    async def analog_write(self, pin: int, value: int) -> None:
        await self._client.publish(Topic.build(Topic.ANALOG, pin), str(value)) if self._client else None

    async def digital_read(self, pin: int, timeout: float = 15) -> int:
        res = await self._publish_and_wait(
            Topic.build(Topic.DIGITAL, pin, "read"),
            b"",
            resp_topic=Topic.build(Topic.DIGITAL, pin, "value"),
            timeout=timeout,
        )
        return int(res.decode())

    async def analog_read(self, pin: int, timeout: float = 15) -> int:
        res = await self._publish_and_wait(
            Topic.build(Topic.ANALOG, pin, "read"),
            b"",
            resp_topic=Topic.build(Topic.ANALOG, pin, "value"),
            timeout=timeout,
        )
        return int(res.decode())

    async def put(self, key: str, value: str | bytes, timeout: float = 15) -> None:
        await self._publish_and_wait(
            Topic.build(Topic.DATASTORE, "put", key),
            value,
            resp_topic=Topic.build(Topic.DATASTORE, "get", key),
            timeout=timeout,
        )

    async def get(self, key: str, timeout: float = 15) -> str:
        res = await self._publish_and_wait(
            Topic.build(Topic.DATASTORE, "get", key, "request"),
            b"",
            resp_topic=Topic.build(Topic.DATASTORE, "get", key),
            timeout=timeout,
        )
        return res.decode()

    async def run_shell_command_async(self, parts: list[str], timeout: float = 15) -> int:
        res = await self._publish_and_wait(
            Topic.build(Topic.SHELL, "run_async"),
            pb.ProcessRunAsync(command=shlex.join(parts)).SerializeToString(),
            resp_topic=Topic.build(Topic.SHELL, "run_async", "response"),
            timeout=timeout,
            content_type=PROTOBUF_CONTENT_TYPE,
        )
        return int(pb.ProcessRunAsyncResponse.FromString(res).pid)

    async def poll_shell_process(self, pid: int, timeout: float = 15) -> ShellPollResponse:
        res = await self._publish_and_wait(
            Topic.build(Topic.SHELL, "poll", pid),
            b"",
            resp_topic=Topic.build(Topic.SHELL, "poll", pid, "response"),
            timeout=timeout,
        )
        packet = pb.ProcessPollResponse.FromString(res)
        return {
            "status_byte": int(packet.status),
            "exit_code": int(packet.exit_code),
            "stdout_chunk": bytes(packet.stdout_data),
            "stderr_chunk": bytes(packet.stderr_data),
            "finished": bool(packet.finished),
            "stdout_truncated": bool(packet.stdout_truncated),
            "stderr_truncated": bool(packet.stderr_truncated),
        }

    async def file_write(self, filename: str, content: str | bytes) -> None:
        (
            await self._client.publish(Topic.build(Topic.FILE, "write", filename.lstrip("/")), content)
            if self._client
            else None
        )

    async def file_read(self, filename: str, timeout: float = 15) -> bytes:
        return await self._publish_and_wait(
            Topic.build(Topic.FILE, "read", filename.lstrip("/")),
            b"",
            resp_topic=Topic.build(Topic.FILE, "read", "response", filename.lstrip("/")),
            timeout=timeout,
        )

    async def file_remove(self, filename: str) -> None:
        if self._client:
            await self._client.publish(Topic.build(Topic.FILE, "remove", filename.lstrip("/")), b"")

    async def mailbox_write(self, message: str | bytes) -> None:
        if self._client:
            await self._client.publish(Topic.build(Topic.MAILBOX, "write"), message)

    async def mailbox_read(self, timeout: float = 5.0) -> bytes | None:
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            return await self._publish_and_wait(
                Topic.build(Topic.MAILBOX, "read"),
                b"",
                resp_topic=Topic.build(Topic.MAILBOX, "incoming"),
                timeout=timeout,
            )
        return None

    async def set_digital_mode(self, pin: int, mode: int) -> None:
        if self._client:
            await self._client.publish(Topic.build(Topic.DIGITAL, pin, "mode"), str(mode))

    async def get_free_memory(self, timeout: float = 15) -> int:
        res = await self._publish_and_wait(
            Topic.build(Topic.SYSTEM, "free_memory", "get"),
            b"",
            resp_topic=Topic.build(Topic.SYSTEM, "free_memory", "value"),
            timeout=timeout,
        )
        return int(res.decode())

    async def enter_bootloader(self) -> None:
        if self._client:
            await self._client.publish(Topic.build(Topic.SYSTEM, "bootloader"), b"", qos=1)

    async def spi_transfer(self, data: bytes, timeout: float = 15) -> bytes:
        return await self._publish_and_wait(
            Topic.build(Topic.SPI, "transfer"),
            data,
            resp_topic=Topic.build(Topic.SPI, "transfer", "resp"),
            timeout=timeout,
        )

    async def spi_begin(self) -> None:
        if self._client:
            await self._client.publish(Topic.build(Topic.SPI, "begin"), b"")

    async def spi_end(self) -> None:
        if self._client:
            await self._client.publish(Topic.build(Topic.SPI, "end"), b"")

    async def spi_config(self, frequency: int, bit_order: int, data_mode: int) -> None:
        if self._client:
            await self._client.publish(
                Topic.build(Topic.SPI, "config"),
                msgspec.json.encode({"frequency": frequency, "bit_order": bit_order, "data_mode": data_mode}),
            )

    def spi(
        self,
        frequency: int = 4000000,
        bit_order: SpiBitOrder | int = SpiBitOrder.MSBFIRST,
        mode: SpiMode | int = SpiMode.MODE0,
    ) -> SpiDevice:
        return SpiDevice(
            self,
            frequency,
            bit_order if isinstance(bit_order, SpiBitOrder) else SpiBitOrder(bit_order),
            mode if isinstance(mode, SpiMode) else SpiMode(mode),
        )
