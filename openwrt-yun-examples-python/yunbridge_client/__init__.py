"""Async MQTT helpers for Yun Bridge example scripts."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional, Union

from aio_mqtt import Client, PublishableMessage, QOSLevel

try:  # aio-mqtt <=0.5
    from aio_mqtt import MqttError  # type: ignore
except ImportError:  # pragma: no cover - compatibility shim for >=0.6
    try:
        from aio_mqtt import Error as MqttError  # type: ignore
    except ImportError:  # pragma: no cover - fallback for future refactors
        from aio_mqtt.error import Error as MqttError  # type: ignore

MQTT_HOST = os.environ.get("YUN_BROKER_IP", "127.0.0.1")
MQTT_PORT = int(os.environ.get("YUN_BROKER_PORT", 1883))
MQTT_TOPIC_PREFIX = "br"
MQTT_USER = os.environ.get("YUN_BROKER_USER")
MQTT_PASS = os.environ.get("YUN_BROKER_PASS")

logger = logging.getLogger(__name__)


def _message(topic: str, payload: bytes) -> PublishableMessage:
    return PublishableMessage(
        topic_name=topic,
        payload=payload,
        qos=QOSLevel.QOS_0,
        retain=False,
    )


@asynccontextmanager
async def get_mqtt_client(
    *,
    host: str = MQTT_HOST,
    port: int = MQTT_PORT,
    username: Optional[str] = MQTT_USER,
    password: Optional[str] = MQTT_PASS,
) -> AsyncGenerator[Client, None]:
    """Provide a connected MQTT client and clean up reliably."""

    client = Client()
    try:
        await client.connect(
            host=host,
            port=port,
            username=username,
            password=password,
        )
        yield client
    except MqttError as error:  # pragma: no cover - connection issues
        logging.error("Error connecting to MQTT broker: %s", error)
        raise
    finally:
        try:
            await client.disconnect()
        except MqttError:
            pass
        finally:
            logging.info("Disconnected from MQTT broker.")


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
        self._client: Optional[Client] = None
        self._response_queues: Dict[str, asyncio.Queue[bytes]] = {}
        self._listener_task: Optional[asyncio.Task[None]] = None
        self._digital_modes: Dict[int, int] = {}

    async def connect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

        client = Client()
        await client.connect(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
        )
        self._client = client
        logger.info("Connected to MQTT broker at %s:%d", self.host, self.port)
        self._digital_modes.clear()
        self._listener_task = asyncio.create_task(self._message_listener())

    async def disconnect(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
            self._listener_task = None

        if self._client is not None:
            await self._client.disconnect()
            self._client = None
            logger.info("Disconnected from MQTT broker.")

    def _ensure_client(self) -> Client:
        client = self._client
        if client is None:
            raise ConnectionError(
                "MQTT client not connected. Call connect() first."
            )
        return client

    async def _message_listener(self) -> None:
        client = self._ensure_client()
        async with client.messages as messages:
            async for message in messages:
                topic = message.topic.value
                payload = message.payload or b""

                handled = False
                for prefix, queue in list(self._response_queues.items()):
                    if topic.startswith(prefix):
                        await queue.put(payload)
                        handled = True
                        break

                if not handled and topic == f"{self.topic_prefix}/console/out":
                    queue = self._response_queues.get(topic)
                    if queue is not None:
                        await queue.put(payload)
                        handled = True

                if not handled:
                    text = payload.decode("utf-8", errors="ignore")
                    logger.debug(
                        "Received unhandled MQTT message: %s -> %s",
                        topic,
                        text,
                    )

    async def _publish_and_wait(
        self,
        pub_topic: str,
        pub_payload: bytes,
        *,
        resp_topic: str,
        timeout: float = 10,
    ) -> bytes:
        client = self._ensure_client()
        response_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        self._response_queues[resp_topic] = response_queue

        try:
            await client.subscribe((resp_topic, QOSLevel.QOS_0))
            await client.publish(_message(pub_topic, pub_payload))
            return await asyncio.wait_for(
                response_queue.get(), timeout=timeout
            )
        finally:
            await client.unsubscribe(resp_topic)
            self._response_queues.pop(resp_topic, None)

    async def digital_write(self, pin: int, value: int) -> None:
        # Ensure the pin is configured as output before writing high/low.
        if self._digital_modes.get(pin) != 1:
            await self.set_digital_mode(pin, 1)
        topic = f"{self.topic_prefix}/d/{pin}"
        payload = str(value).encode("utf-8")
        await self._ensure_client().publish(_message(topic, payload))
        logger.debug("digital_write(%d, %d) -> %s", pin, value, topic)

    async def digital_read(self, pin: int, timeout: float = 10) -> int:
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/d/{pin}/read",
            b"",
            resp_topic=f"{self.topic_prefix}/d/{pin}/value",
            timeout=timeout,
        )
        return int(response.decode("utf-8"))

    async def analog_read(self, pin: int, timeout: float = 10) -> int:
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/a/{pin}/read",
            b"",
            resp_topic=f"{self.topic_prefix}/a/{pin}/value",
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
        await self._ensure_client().publish(
            _message(topic, str(mode_value).encode("utf-8"))
        )
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
            f"{self.topic_prefix}/datastore/get/{key}",
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
        logger.warning(
            "run_sketch_command falls back to a synchronous shell command via "
            "MQTT."
        )
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/run",
            " ".join(command_parts).encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/sh/response",
            timeout=timeout,
        )
        return response

    async def run_shell_command_async(
        self, command_parts: List[str], timeout: float = 10
    ) -> int:
        response = await self._publish_and_wait(
            f"{self.topic_prefix}/sh/run_async",
            " ".join(command_parts).encode("utf-8"),
            resp_topic=f"{self.topic_prefix}/sh/run_async/response",
            timeout=timeout,
        )
        return int(response.decode("utf-8"))

    async def console_write(self, message: str) -> None:
        topic = f"{self.topic_prefix}/console/in"
        await self._ensure_client().publish(
            _message(topic, message.encode("utf-8"))
        )
        logger.debug("console_write('%s')", message)

    async def console_read_async(self) -> Optional[str]:
        topic = f"{self.topic_prefix}/console/out"

        if topic not in self._response_queues:
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
            self._response_queues[topic] = queue
            await self._ensure_client().subscribe((topic, QOSLevel.QOS_0))
            logger.debug("Subscribed to console output topic: %s", topic)

        try:
            payload = await asyncio.wait_for(
                self._response_queues[topic].get(), timeout=0.1
            )
        except asyncio.TimeoutError:
            return None
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Error reading from console queue")
            return None

        return payload.decode("utf-8", errors="ignore")

    async def file_write(
        self, filename: str, content: Union[str, bytes]
    ) -> None:
        payload = (
            content.encode("utf-8") if isinstance(content, str) else content
        )
        topic = f"{self.topic_prefix}/file/write/{filename}"
        await self._ensure_client().publish(_message(topic, payload))
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
        await self._ensure_client().publish(_message(topic, b""))
        logger.debug("file_remove('%s')", filename)

    async def mailbox_write(self, message: Union[str, bytes]) -> None:
        payload = (
            message.encode("utf-8") if isinstance(message, str) else message
        )
        topic = f"{self.topic_prefix}/mailbox/write"
        await self._ensure_client().publish(_message(topic, payload))
        logger.debug("mailbox_write(%d bytes)", len(payload))
