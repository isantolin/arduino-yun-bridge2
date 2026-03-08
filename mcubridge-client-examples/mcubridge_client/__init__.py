"""Async MQTT helpers for MCU Bridge example scripts."""

from __future__ import annotations

import asyncio
import logging
import secrets
import ssl
from contextlib import AsyncExitStack
from typing import Any, TypedDict, Union

import aiomqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .protocol import Topic
from .structures import AnalogReadResponsePacket, DigitalReadResponsePacket


class BridgeConfig(TypedDict):
    """Configuration for the MCU Bridge MQTT connection."""

    host: str
    port: int
    username: Union[str, None]
    password: Union[str, None]
    tls_insecure: bool


def build_bridge_args(
    host: Union[str, None] = None,
    port: Union[int, None] = None,
    user: Union[str, None] = None,
    password: Union[str, None] = None,
    tls_insecure: bool = False,
) -> BridgeConfig:
    """Build a BridgeConfig dictionary with defaults."""
    return {
        "host": host or "127.0.0.1",
        "port": port or 1883,
        "username": user,
        "password": password,
        "tls_insecure": tls_insecure,
    }


def dump_client_env(logger: logging.Logger) -> None:
    """Dump client environment (stub)."""


def _payload_bytes(payload: Any) -> bytes:
    """Ensure payload is bytes."""
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return bytes(payload)


class BridgeClient:
    """Async MQTT client for interacting with the MCU Bridge."""

    def __init__(
        self,
        host: str,
        port: int,
        username: Union[str, None] = None,
        password: Union[str, None] = None,
        tls_insecure: bool = False,
        prefix: str = "br",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tls_insecure = tls_insecure
        self.prefix = prefix
        self._stack = AsyncExitStack()
        self._client: Union[aiomqtt.Client, None] = None
        self._response_routes: dict[str, list[tuple[asyncio.Queue[aiomqtt.message.Message], bool]]] = {}
        self._correlation_routes: dict[bytes, asyncio.Queue[bytes]] = {}

    async def __aenter__(self) -> BridgeClient:
        ctx = ssl.create_default_context()
        if self.tls_insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        self._client = aiomqtt.Client(
            self.host,
            self.port,
            username=self.username,
            password=self.password,
            tls_context=ctx if self.port == 8883 else None,
        )
        await self._stack.enter_async_context(self._client)
        # Type ignore because _client is only None before __aenter__
        self._stack.enter_context(self._client.messages.proxy())  # type: ignore
        asyncio.create_task(self._listen_loop())
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._stack.aclose()

    async def _listen_loop(self) -> None:
        if self._client is None:
            return
        async for message in self._client.messages:
            await self._handle_message(message)

    async def _handle_message(self, message: aiomqtt.message.Message) -> None:
        topic = str(message.topic)
        props = getattr(message, "properties", None)
        correlation = getattr(props, "CorrelationData", None)

        if correlation and correlation in self._correlation_routes:
            q = self._correlation_routes.pop(correlation)
            q.put_nowait(message.payload)

        for prefix, queues in list(self._response_routes.items()):
            if topic.startswith(prefix):
                for q, _ in queues:
                    q.put_nowait(message)

    async def _publish_and_wait(
        self,
        cmd_topic: str,
        payload: bytes,
        resp_topic: Union[str, list[str]],
        timeout: int = 10,
    ) -> bytes:
        if self._client is None:
            raise RuntimeError("Client not connected")
        q: asyncio.Queue[bytes] = asyncio.Queue()
        correlation = secrets.token_bytes(8)
        self._correlation_routes[correlation] = q
        topics = [resp_topic] if isinstance(resp_topic, str) else list(resp_topic)
        for t in topics:
            await self._client.subscribe(t)

        props = Properties(PacketTypes.PUBLISH)
        props.CorrelationData = correlation
        props.ResponseTopic = topics[0]

        await self._client.publish(cmd_topic, payload, properties=props)
        try:
            res = await asyncio.wait_for(q.get(), timeout)
            return res
        finally:
            self._correlation_routes.pop(correlation, None)

    async def digital_read(self, pin: int, timeout: int = 10) -> int:
        """Read a digital pin."""
        res = await self._publish_and_wait(
            Topic.command("d", pin, "read"),
            b"",
            Topic.status("d", pin, "value"),
            timeout,
        )
        try:
            return int(DigitalReadResponsePacket.decode(res).value)
        except Exception:
            return int(res.decode())

    async def analog_read(self, pin: int, timeout: int = 10) -> int:
        """Read an analog pin."""
        res = await self._publish_and_wait(
            Topic.command("a", pin, "read"),
            b"",
            Topic.status("a", pin, "value"),
            timeout,
        )
        try:
            return int(AnalogReadResponsePacket.decode(res).value)
        except Exception:
            return int(res.decode())

    async def digital_write(self, pin: int, val: Union[int, bool]) -> None:
        """Write to a digital pin."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("d", pin), str(int(val)).encode(), qos=1)

    async def mailbox_write(self, msg: Any) -> None:
        """Write to the mailbox."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("mailbox", "write"), _payload_bytes(msg), qos=1)

    async def mailbox_read(self, timeout: int = 10) -> Union[bytes, None]:
        """Read from the mailbox."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        q: asyncio.Queue[aiomqtt.message.Message] = asyncio.Queue()
        prefix = Topic.status("mailbox", "incoming")
        if prefix not in self._response_routes:
            self._response_routes[prefix] = []
        route = (q, True)
        self._response_routes[prefix].append(route)
        await self._client.subscribe(prefix)
        try:
            msg = await asyncio.wait_for(q.get(), timeout)
            return _payload_bytes(msg.payload)
        except Exception:
            return None
        finally:
            self._response_routes[prefix].remove(route)

    async def run_shell_command_async(self, cmd: str) -> bytes:
        """Run a shell command asynchronously."""
        return await self._publish_and_wait(
            Topic.command("sh", "run_async"),
            cmd.encode(),
            Topic.status("sh", "run_async", "response"),
        )

    async def write_file(self, path: str, content: Any) -> None:
        """Write a file."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("file", "write", path), _payload_bytes(content), qos=1)

    async def read_file(self, path: str, timeout: int = 10) -> bytes:
        """Read a file."""
        return await self._publish_and_wait(
            Topic.command("file", "read", path),
            b"",
            Topic.status("file", "read", "response"),
            timeout,
        )

    async def remove_file(self, path: str) -> None:
        """Remove a file."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("file", "remove", path), b"", qos=1)

    async def put(self, k: str, v: str) -> None:
        """Put a key-value pair in the datastore."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("datastore", "put", k), v.encode(), qos=1)

    async def get(self, k: str, timeout: int = 5) -> str:
        """Get a value from the datastore."""
        res = await self._publish_and_wait(
            Topic.command("datastore", "get", k),
            b"",
            Topic.status("datastore", k),
            timeout,
        )
        return res.decode()

    async def console_write(self, msg: Any) -> None:
        """Write to the console."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("console", "in"), _payload_bytes(msg), qos=1)

    async def set_digital_mode(self, pin: int, mode: int) -> None:
        """Set digital pin mode."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("d", pin, "mode"), str(mode).encode(), qos=1)

    async def analog_write(self, pin: int, val: int) -> None:
        """Write an analog value."""
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.publish(Topic.command("a", pin), str(val).encode(), qos=1)
