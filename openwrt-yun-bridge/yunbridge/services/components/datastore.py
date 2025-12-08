"""Datastore component for MCU/Linux interactions."""
from __future__ import annotations

import logging
import struct

from aiomqtt.client import Message as MQTTMessage
from ...mqtt.messages import QueuedPublish
from ...state.context import RuntimeState
from ...config.settings import RuntimeConfig
from ...protocol.topics import Topic, topic_path
from .base import BridgeContext
from yunbridge.rpc.protocol import (
    DATASTORE_VALUE_LEN_FORMAT,
    DATASTORE_VALUE_LEN_SIZE,
    Command,
    Status,
)

logger = logging.getLogger("yunbridge.datastore")


class DatastoreComponent:
    """Encapsulate datastore behaviour for BridgeService."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx

    async def handle_put(self, payload: bytes) -> bool:
        """Process CMD_DATASTORE_PUT received from the MCU."""
        if len(payload) < 2:
            logger.warning(
                "Malformed DATASTORE_PUT payload: too short (%d bytes)",
                len(payload),
            )
            return False

        key_len = payload[0]
        cursor = 1
        if len(payload) < cursor + key_len + DATASTORE_VALUE_LEN_SIZE:
            logger.warning(
                "Malformed DATASTORE_PUT payload: missing key/value data.",
            )
            return False

        key_bytes = payload[cursor:cursor + key_len]
        cursor += key_len
        value_len = payload[cursor]
        cursor += DATASTORE_VALUE_LEN_SIZE

        remaining = len(payload) - cursor
        if remaining < value_len:
            logger.warning(
                "Malformed DATASTORE_PUT payload: value length mismatch.",
            )
            return False

        value_bytes = payload[cursor:cursor + value_len]
        key = key_bytes.decode("utf-8", errors="ignore")
        value = value_bytes.decode("utf-8", errors="ignore")

        self.state.datastore[key] = value
        await self._publish_value(key, value_bytes)
        return True

    async def handle_get_request(self, payload: bytes) -> bool:
        """Handle CMD_DATASTORE_GET initiated by the MCU."""
        if len(payload) < 1:
            logger.warning(
                "Malformed DATASTORE_GET payload: too short (%d bytes)",
                len(payload),
            )
            await self.ctx.send_frame(
                Status.MALFORMED.value,
                b"data_get_short",
            )
            return False

        key_len = payload[0]
        if len(payload) < 1 + key_len:
            logger.warning(
                "Malformed DATASTORE_GET payload: missing key bytes",
            )
            await self.ctx.send_frame(Status.MALFORMED.value, b"data_get_key")
            return False

        key_bytes = payload[1:1 + key_len]
        key = key_bytes.decode("utf-8", errors="ignore")
        value = self.state.datastore.get(key, "")
        value_bytes = value.encode("utf-8")
        if len(value_bytes) > 255:
            logger.warning(
                "Datastore value truncated for key %s (%d bytes)",
                key,
                len(value_bytes),
            )
            value_bytes = value_bytes[:255]

        response_payload = struct.pack(
            DATASTORE_VALUE_LEN_FORMAT,
            len(value_bytes),
        ) + value_bytes

        send_ok = await self.ctx.send_frame(
            Command.CMD_DATASTORE_GET_RESP.value,
            response_payload,
        )
        if send_ok:
            await self._publish_value(key, value_bytes)
        return send_ok

    async def handle_mqtt(
        self,
        identifier: str,
        remainder: list[str],
        payload: bytes,
        payload_str: str,
        inbound: MQTTMessage | None = None,
    ) -> None:
        is_request = False
        parts = remainder.copy()
        if identifier == "get" and parts and parts[-1] == "request":
            parts = parts[:-1]
            is_request = True

        key = "/".join(parts)
        if identifier == "put":
            if not parts:
                logger.debug("Ignoring datastore put without key")
                return
            await self._handle_mqtt_put(key, payload_str, inbound)
            return

        if identifier == "get":
            if not key:
                logger.debug("Ignoring datastore get without key")
                return
            await self._handle_mqtt_get(key, is_request, inbound)
            return

        logger.debug("Unknown datastore action '%s'", identifier)

    async def _handle_mqtt_put(
        self,
        key: str,
        value_text: str,
        inbound: MQTTMessage | None,
    ) -> None:
        key_bytes = key.encode("utf-8")
        value_bytes = value_text.encode("utf-8")

        if len(key_bytes) > 255 or len(value_bytes) > 255:
            logger.warning(
                "Datastore payload too large. key=%d value=%d",
                len(key_bytes),
                len(value_bytes),
            )
            return

        self.state.datastore[key] = value_text
        await self._publish_value(
            key,
            value_bytes,
            reply_context=inbound,
        )

    async def _handle_mqtt_get(
        self,
        key: str,
        is_request: bool,
        inbound: MQTTMessage | None,
    ) -> None:
        key_bytes = key.encode("utf-8")
        if len(key_bytes) > 255:
            logger.warning(
                "Datastore key too large for GET request (%d bytes)",
                len(key_bytes),
            )
            return

        cached_value = self.state.datastore.get(key)
        if cached_value is None:
            if is_request:
                await self._publish_value(
                    key,
                    b"",
                    reply_context=inbound,
                    error_reason="datastore-miss",
                )
            else:
                logger.debug(
                    "Datastore GET for '%s' has no cached value", key
                )
            return

        await self._publish_value(
            key,
            cached_value.encode("utf-8"),
            reply_context=inbound,
        )

    async def _publish_value(
        self,
        key: str,
        value: bytes,
        *,
        reply_context: MQTTMessage | None = None,
        error_reason: str | None = None,
    ) -> None:
        key_segments = tuple(segment for segment in key.split("/") if segment)
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.DATASTORE,
            "get",
            *key_segments,
        )
        properties: list[tuple[str, str]] = [
            ("bridge-datastore-key", key)
        ]
        if error_reason:
            properties.append(("bridge-error", error_reason))
        message = QueuedPublish(
            topic_name=topic_name,
            payload=value,
            message_expiry_interval=60,
            content_type="text/plain; charset=utf-8",
            user_properties=tuple(properties),
        )
        await self.ctx.enqueue_mqtt(
            message,
            reply_context=reply_context,
        )


__all__ = ["DatastoreComponent"]
