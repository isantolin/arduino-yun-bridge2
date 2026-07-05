"""Minimalistic Async Client for MCU Bridge."""

from __future__ import annotations
from . import mcubridge_pb2 as pb

import asyncio
import logging
import os
import secrets
import shlex
import ssl
from typing import TypedDict

from .definitions import (
    MqttQueuedPublish,
    SpiBitOrder,
    SpiMode,
    build_bridge_args,
)
from mcubridge.protocol.structures import create_queued_publish
from .env import dump_client_env
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
    "Command",
    "Topic",
    "MqttQueuedPublish",
]

logger = logging.getLogger(__name__)
PROTOBUF_CONTENT_TYPE = "application/x-protobuf"


class ShellPollResponse(TypedDict, total=False):
    status_byte: int
    exit_code: int
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool


class Bridge:
    """Zero-Boilerplate high-level client for MCU Bridge (SIL-2)."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        topic_prefix: str = "mcubridge",
        username: str | None = None,
        password: str | None = None,
        tls_context: ssl.SSLContext | None = None,
        socket_path: str = "/var/run/mcubridge.sock",
    ) -> None:
        self.topic_prefix = topic_prefix
        Topic.PREFIX = topic_prefix
        self.socket_path = os.environ.get("MCUBRIDGE_SOCKET_PATH") or socket_path

        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._correlation_routes: dict[bytes, asyncio.Queue[pb.MqttQueuedPublish]] = {}
        self._console_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._listener_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        if self.writer:
            await self.disconnect()

        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
        self._listener_task = asyncio.create_task(self._message_listener())
        logger.info("Connected to local IPC socket: %s", self.socket_path)

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
            self.writer = None
        self.reader = None
        logger.info("Disconnected from local IPC socket.")

    async def _message_listener(self) -> None:
        if not self.reader:
            return
        try:
            while True:
                len_bytes = await self.reader.readexactly(4)
                length = int.from_bytes(len_bytes, byteorder="big")
                data = await self.reader.readexactly(length)
                message = pb.MqttQueuedPublish.FromString(data)

                correlation = message.correlation_data if message.HasField("correlation_data") else None
                if correlation and (queue := self._correlation_routes.pop(correlation, None)):
                    queue.put_nowait(message)
                elif "console" in message.topic_name:
                    self._console_queue.put_nowait(message.payload if message.payload else b"")
        except (asyncio.IncompleteReadError, OSError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.error("IPC listener error", exc_info=e)

    async def _publish_and_wait(
        self,
        topic: str,
        payload: bytes | str,
        *,
        resp_topic: str | None = None,
        timeout: float = 15,
        content_type: str | None = None,
    ) -> bytes:
        if not self.writer:
            raise ConnectionError("Not connected")

        correlation = secrets.token_bytes(12)
        queue: asyncio.Queue[pb.MqttQueuedPublish] = asyncio.Queue(maxsize=1)
        self._correlation_routes[correlation] = queue

        try:
            msg = create_queued_publish(
                topic_name=topic,
                payload=payload.encode() if isinstance(payload, str) else payload,
                content_type=content_type,
            )
            msg.correlation_data = correlation

            data = msg.SerializeToString()
            self.writer.write(len(data).to_bytes(4, byteorder="big"))
            self.writer.write(data)
            await self.writer.drain()

            delivered = await asyncio.wait_for(queue.get(), timeout=timeout)
            return delivered.payload if delivered.payload else b""
        finally:
            self._correlation_routes.pop(correlation, None)

    async def _publish(self, topic: str | Topic, payload: bytes) -> None:
        if not self.writer:
            raise ConnectionError("Not connected")

        msg = create_queued_publish(
            topic_name=str(topic),
            payload=payload,
        )
        data = msg.SerializeToString()
        self.writer.write(len(data).to_bytes(4, byteorder="big"))
        self.writer.write(data)
        await self.writer.drain()

    # --- Declarative API (Eradicates manual methods) ---

    async def console_write(self, data: str | bytes) -> None:
        payload = data if isinstance(data, bytes) else data.encode()
        await self._publish(Topic.build(Topic.CONSOLE, "in"), payload)

    async def console_read_async(self) -> str | None:
        try:
            payload = await asyncio.wait_for(self._console_queue.get(), timeout=0.1)
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                return f"<hex:{payload.hex()}>"
        except TimeoutError:
            return None

    async def digital_write(self, pin: int, value: int) -> None:
        await self._publish(Topic.build(Topic.DIGITAL, pin), str(value).encode())

    async def analog_write(self, pin: int, value: int) -> None:
        await self._publish(Topic.build(Topic.ANALOG, pin), str(value).encode())

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
        return pb.ProcessRunAsyncResponse.FromString(res).pid

    async def poll_shell_process(self, pid: int, timeout: float = 15) -> ShellPollResponse:
        res = await self._publish_and_wait(
            Topic.build(Topic.SHELL, "poll", pid),
            b"",
            resp_topic=Topic.build(Topic.SHELL, "poll", pid, "response"),
            timeout=timeout,
        )
        packet = pb.ProcessPollResponse.FromString(res)
        return {
            "status_byte": packet.status,
            "exit_code": packet.exit_code,
            "stdout_chunk": packet.stdout_data,
            "stderr_chunk": packet.stderr_data,
            "finished": packet.finished,
            "stdout_truncated": packet.stdout_truncated,
            "stderr_truncated": packet.stderr_truncated,
        }

    async def file_write(self, filename: str, content: str | bytes) -> None:
        await self._publish(
            Topic.build(Topic.FILE, "write", filename.lstrip("/")),
            content if isinstance(content, bytes) else content.encode(),
        )

    async def file_read(self, filename: str, timeout: float = 15) -> bytes:
        return await self._publish_and_wait(
            Topic.build(Topic.FILE, "read", filename.lstrip("/")),
            b"",
            resp_topic=Topic.build(Topic.FILE, "read", "response", filename.lstrip("/")),
            timeout=timeout,
        )

    async def file_remove(self, filename: str) -> None:
        await self._publish(Topic.build(Topic.FILE, "remove", filename.lstrip("/")), b"")

    async def mailbox_write(self, message: str | bytes) -> None:
        await self._publish(
            Topic.build(Topic.MAILBOX, "write"), message if isinstance(message, bytes) else message.encode()
        )

    async def mailbox_read(self, timeout: float = 5.0) -> bytes | None:
        try:
            return await self._publish_and_wait(
                Topic.build(Topic.MAILBOX, "read"),
                b"",
                resp_topic=Topic.build(Topic.MAILBOX, "incoming"),
                timeout=timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return None

    async def set_digital_mode(self, pin: int, mode: int) -> None:
        await self._publish(Topic.build(Topic.DIGITAL, pin, "mode"), str(mode).encode())

    async def get_free_memory(self, timeout: float = 15) -> int:
        res = await self._publish_and_wait(
            Topic.build(Topic.SYSTEM, "free_memory", "get"),
            b"",
            resp_topic=Topic.build(Topic.SYSTEM, "free_memory", "value"),
            timeout=timeout,
        )
        return int(res.decode())

    async def enter_bootloader(self) -> None:
        await self._publish(Topic.build(Topic.SYSTEM, "bootloader"), b"")

    async def spi_transfer(self, data: bytes, timeout: float = 15) -> bytes:
        return await self._publish_and_wait(
            Topic.build(Topic.SPI, "transfer"),
            data,
            resp_topic=Topic.build(Topic.SPI, "transfer", "resp"),
            timeout=timeout,
        )

    async def spi_begin(self) -> None:
        await self._publish(Topic.build(Topic.SPI, "begin"), b"")

    async def spi_end(self) -> None:
        await self._publish(Topic.build(Topic.SPI, "end"), b"")

    async def spi_config(self, frequency: int, bit_order: int, data_mode: int) -> None:
        config = pb.SpiConfig(frequency=frequency, bit_order=bit_order, data_mode=data_mode)
        await self._publish(
            Topic.build(Topic.SPI, "config"),
            config.SerializeToString(),
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
