"""System component handling MCU system requests and MQTT interactions."""

from __future__ import annotations

import collections
import logging
from typing import Deque

from aiomqtt.message import Message as MQTTMessage
from yunbridge.rpc.protocol import Command

from ...mqtt.messages import QueuedPublish
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
from ...protocol.topics import Topic, topic_path
from .base import BridgeContext

logger = logging.getLogger("yunbridge.system")


class SystemComponent:
    """Encapsulate MCU system information flows."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx
        self._pending_free_memory: Deque[MQTTMessage] = collections.deque()
        self._pending_version: Deque[MQTTMessage] = collections.deque()
        self._pending_tx_debug: Deque[MQTTMessage] = collections.deque()

    async def request_mcu_version(self) -> bool:
        send_ok = await self.ctx.send_frame(Command.CMD_GET_VERSION.value, b"")
        if send_ok:
            self.state.mcu_version = None
        return send_ok

    async def handle_get_free_memory_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning("Malformed GET_FREE_MEMORY_RESP payload: %s", payload.hex())
            return

        free_memory = int.from_bytes(payload, "big")
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "free_memory",
            "value",
        )
        message = QueuedPublish(
            topic_name=topic,
            payload=str(free_memory).encode("utf-8"),
            message_expiry_interval=10,
            content_type="text/plain; charset=utf-8",
        )
        reply_context = None
        if self._pending_free_memory:
            reply_context = self._pending_free_memory.popleft()
        if reply_context is not None:
            await self.ctx.enqueue_mqtt(
                message,
                reply_context=reply_context,
            )
        await self.ctx.enqueue_mqtt(message)

    async def handle_get_version_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning("Malformed GET_VERSION_RESP payload: %s", payload.hex())
            return

        major, minor = payload[0], payload[1]
        self.state.mcu_version = (major, minor)
        reply_context = None
        if self._pending_version:
            reply_context = self._pending_version.popleft()
        await self._publish_version((major, minor), reply_context)
        logger.info("MCU firmware version reported as %d.%d", major, minor)

    async def handle_get_tx_debug_snapshot_resp(self, payload: bytes) -> None:
        if len(payload) != 9:
            logger.warning(
                "Malformed GET_TX_DEBUG_SNAPSHOT_RESP payload: %s", payload.hex()
            )
            return

        pending_tx_count = payload[0]
        awaiting_ack = bool(payload[1])
        retry_count = payload[2]
        last_command_id = int.from_bytes(payload[3:5], "big")
        last_send_millis = int.from_bytes(payload[5:9], "big")

        data = {
            "pending_tx_count": pending_tx_count,
            "awaiting_ack": awaiting_ack,
            "retry_count": retry_count,
            "last_command_id": last_command_id,
            "last_send_millis": last_send_millis,
        }

        import json

        payload_json = json.dumps(data).encode("utf-8")

        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "tx_debug",
            "value",
        )
        message = QueuedPublish(
            topic_name=topic,
            payload=payload_json,
            message_expiry_interval=10,
            content_type="application/json",
        )

        reply_context = None
        if self._pending_tx_debug:
            reply_context = self._pending_tx_debug.popleft()

        if reply_context is not None:
            await self.ctx.enqueue_mqtt(
                message,
                reply_context=reply_context,
            )
        await self.ctx.enqueue_mqtt(message)

    async def handle_mqtt(
        self,
        identifier: str,
        remainder: list[str],
        inbound: MQTTMessage | None = None,
    ) -> bool:
        if identifier == "free_memory" and remainder and remainder[0] == "get":
            if inbound is not None:
                self._pending_free_memory.append(inbound)
            await self.ctx.send_frame(Command.CMD_GET_FREE_MEMORY.value, b"")
            return True

        if identifier == "tx_debug" and remainder and remainder[0] == "get":
            if inbound is not None:
                self._pending_tx_debug.append(inbound)
            await self.ctx.send_frame(Command.CMD_GET_TX_DEBUG_SNAPSHOT.value, b"")
            return True

        if identifier == "version" and remainder and remainder[0] == "get":
            cached_version = self.state.mcu_version
            if cached_version is not None and inbound is not None:
                await self._publish_version(cached_version, inbound)
            else:
                if inbound is not None:
                    self._pending_version.append(inbound)
            await self.request_mcu_version()
            if cached_version is not None:
                await self._publish_version(cached_version)
            return True

        return False

    async def _publish_version(
        self,
        version: tuple[int, int],
        reply_context: MQTTMessage | None = None,
    ) -> None:
        major, minor = version
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "version",
            "value",
        )
        message = QueuedPublish(
            topic_name=topic,
            payload=f"{major}.{minor}".encode(),
            message_expiry_interval=60,
            content_type="text/plain; charset=utf-8",
        )
        if reply_context is not None:
            await self.ctx.enqueue_mqtt(
                message,
                reply_context=reply_context,
            )
        await self.ctx.enqueue_mqtt(message)


__all__ = ["SystemComponent"]
