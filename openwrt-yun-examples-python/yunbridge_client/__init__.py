"""Utilities for the Yun Bridge examples, using an async MQTT client."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, Dict, List, Union

import aio_mqtt
from aio_mqtt import Client, MqttError, Message

# Centralized MQTT configuration for all examples
# The broker IP can be overridden with an environment variable
MQTT_HOST = os.environ.get("YUN_BROKER_IP", "192.168.15.28")
MQTT_PORT = int(os.environ.get("YUN_BROKER_PORT", 1883))
MQTT_TOPIC_PREFIX = "br"

logger = logging.getLogger(__name__)


@asynccontextmanager
async def get_mqtt_client() -> AsyncGenerator[Client, None]: # Used Client here
    """An async context manager to simplify aio_mqtt.Client setup and connection."""
    try:
        async with Client(host=MQTT_HOST, port=MQTT_PORT) as client:
            yield client
    except MqttError as error:
        logging.error("Error connecting to MQTT broker: %s", error)
        raise
    finally:
        logging.info("Disconnected from MQTT broker.")


class Bridge:
    def __init__(self, host: str = MQTT_HOST, port: int = MQTT_PORT, topic_prefix: str = MQTT_TOPIC_PREFIX):
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix
        self._client: Optional[Client] = None # Used Client here
        self._response_queues: Dict[str, asyncio.Queue[bytes]] = {}
        self._listener_task: Optional[asyncio.Task[None]] = None

    async def connect(self):
        if self._client:
            await self._client.disconnect()
        self._client = Client(host=self.host, port=self.port)
        await self._client.connect()
        logger.info("Connected to MQTT broker at %s:%d", self.host, self.port)
        self._listener_task = asyncio.create_task(self._message_listener())

    async def disconnect(self):
        if self._listener_task:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
            self._listener_task = None
        if self._client:
            await self._client.disconnect()
            self._client = None
            logger.info("Disconnected from MQTT broker.")

    async def _message_listener(self):
        if not self._client:
            return # Should not happen if connect() was called
        async with self._client.messages as messages:
            async for message in messages:
                topic = message.topic.value
                payload: bytes = message.payload # Explicitly typed payload

                # Check for responses and put into appropriate queue
                handled = False
                for prefix, queue in self._response_queues.items():
                    if topic.startswith(prefix):
                        await queue.put(payload)
                        handled = True
                        break
                
                # Handle console output specifically if not already handled by a response queue
                if not handled and topic == f"{self.topic_prefix}/console/out":
                    if topic in self._response_queues:
                        await self._response_queues[topic].put(payload)
                        handled = True

                if not handled:
                    logger.debug("Received unhandled MQTT message: %s -> %s", topic, payload.decode(errors='ignore'))

    async def _publish_and_wait_for_response(self, pub_topic: str, pub_payload: bytes = b"", resp_topic_prefix: str = "", timeout: int = 10) -> bytes:
        if not self._client:
            raise ConnectionError("MQTT client not connected. Call connect() first.")

        response_queue = asyncio.Queue(maxsize=1)
        self._response_queues[resp_topic_prefix] = response_queue

        try:
            # Subscribe to the response topic before publishing
            await self._client.subscribe(resp_topic_prefix)
            await self._client.publish(pub_topic, pub_payload)
            response = await asyncio.wait_for(response_queue.get(), timeout=timeout)
            return response
        finally:
            # Unsubscribe after receiving response or timeout
            await self._client.unsubscribe(resp_topic_prefix)
            del self._response_queues[resp_topic_prefix]

    # --- Implement methods for all_features_test.py ---

    async def digital_write(self, pin: int, value: int):
        if not self._client:
            raise ConnectionError("MQTT client not connected. Call connect() first.")
        topic = f"{self.topic_prefix}/d/{pin}"
        payload = str(value).encode("utf-8")
        await self._client.publish(topic, payload)
        logger.debug("digital_write(%d, %d) -> %s", pin, value, topic)

    async def digital_read(self, pin: int, timeout: int = 10) -> int:
        pub_topic = f"{self.topic_prefix}/d/{pin}/read"
        resp_topic_prefix = f"{self.topic_prefix}/d/{pin}/value"
        response = await self._publish_and_wait_for_response(pub_topic, resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        return int(response.decode("utf-8"))

    async def analog_read(self, pin: int, timeout: int = 10) -> int:
        pub_topic = f"{self.topic_prefix}/a/{pin}/read"
        resp_topic_prefix = f"{self.topic_prefix}/a/{pin}/value"
        response = await self._publish_and_wait_for_response(pub_topic, resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        return int(response.decode("utf-8"))

    async def put(self, key: str, value: str, timeout: int = 10):
        pub_topic = f"{self.topic_prefix}/datastore/put/{key}"
        resp_topic_prefix = f"{self.topic_prefix}/datastore/get/{key}" # Daemon publishes to this after put
        await self._publish_and_wait_for_response(pub_topic, value.encode("utf-8"), resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        logger.debug("datastore put('%s', '%s')", key, value)

    async def get(self, key: str, timeout: int = 10) -> str:
        pub_topic = f"{self.topic_prefix}/datastore/get/{key}"
        resp_topic_prefix = f"{self.topic_prefix}/datastore/get/{key}"
        response = await self._publish_and_wait_for_response(pub_topic, resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        return response.decode("utf-8")

    async def get_free_memory(self, timeout: int = 10) -> int:
        pub_topic = f"{self.topic_prefix}/system/free_memory/get"
        resp_topic_prefix = f"{self.topic_prefix}/system/free_memory/value"
        response = await self._publish_and_wait_for_response(pub_topic, resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        return int(response.decode("utf-8"))

    async def run_sketch_command(self, command_parts: List[str], timeout: int = 10) -> bytes:
        logger.warning("run_sketch_command is mapped to synchronous shell command via MQTT, returning full output.")
        command_str = " ".join(command_parts)
        pub_topic = f"{self.topic_prefix}/sh/run"
        resp_topic_prefix = f"{self.topic_prefix}/sh/response"
        response = await self._publish_and_wait_for_response(pub_topic, command_str.encode("utf-8"), resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        return response

    async def run_shell_command_async(self, command_parts: List[str], timeout: int = 10) -> int:
        command_str = " ".join(command_parts)
        pub_topic = f"{self.topic_prefix}/sh/run_async"
        resp_topic_prefix = f"{self.topic_prefix}/sh/run_async/response/{response.decode('utf-8')}"
        response = await self._publish_and_wait_for_response(pub_topic, command_str.encode("utf-8"), resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        return int(response.decode("utf-8")) # Expecting PID as string

    async def console_write(self, message: str):
        if not self._client:
            raise ConnectionError("MQTT client not connected. Call connect() first.")
        topic = f"{self.topic_prefix}/console/in"
        await self._client.publish(topic, message.encode("utf-8"))
        logger.debug("console_write('%s')", message)

    async def console_read_async(self) -> Optional[str]:
        if not self._client:
            raise ConnectionError("MQTT client not connected. Call connect() first.")
        
        topic = f"{self.topic_prefix}/console/out"
        
        # Ensure we are subscribed to the console output topic
        if topic not in self._response_queues:
            response_queue = asyncio.Queue(maxsize=100) # Use a larger queue for console output
            self._response_queues[topic] = response_queue
            await self._client.subscribe(topic)
            logger.debug("Subscribed to console output topic: %s", topic)

        try:
            # Wait for a message with a short timeout to allow non-blocking behavior
            message_payload = await asyncio.wait_for(self._response_queues[topic].get(), timeout=0.1)
            return message_payload.decode("utf-8", errors="ignore")
        except asyncio.TimeoutError:
            return None # No message received within the timeout
        except Exception as e:
            logger.error("Error reading from console queue: %s", e)
            return None

    async def file_write(self, filename: str, content: Union[str, bytes]):
        if not self._client:
            raise ConnectionError("MQTT client not connected. Call connect() first.")
        topic = f"{self.topic_prefix}/file/write/{filename}"
        payload = content.encode("utf-8") if isinstance(content, str) else content
        await self._client.publish(topic, payload)
        logger.debug("file_write('%s', %d bytes)", filename, len(payload))

    async def file_read(self, filename: str, timeout: int = 10) -> bytes:
        pub_topic = f"{self.topic_prefix}/file/read/{filename}"
        resp_topic_prefix = f"{self.topic_prefix}/file/read/response/{filename}"
        response = await self._publish_and_wait_for_response(pub_topic, resp_topic_prefix=resp_topic_prefix, timeout=timeout)
        return response

    async def file_remove(self, filename: str):
        if not self._client:
            raise ConnectionError("MQTT client not connected. Call connect() first.")
        topic = f"{self.topic_prefix}/file/remove/{filename}"
        await self._client.publish(topic, b"")
        logger.debug("file_remove('%s')", filename)

    async def mailbox_write(self, message: Union[str, bytes]):
        if not self._client:
            raise ConnectionError("MQTT client not connected. Call connect() first.")
        topic = f"{self.topic_prefix}/mailbox/write"
        payload = message.encode("utf-8") if isinstance(message, str) else message
        await self._client.publish(topic, payload)
        logger.debug("mailbox_write(%d bytes)", len(payload))
