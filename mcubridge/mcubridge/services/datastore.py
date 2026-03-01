"""Datastore component for MCU/Linux interactions."""

from __future__ import annotations

import logging
from typing import Any

from aiomqtt.message import Message
from construct import ConstructError
from mcubridge.protocol.protocol import (
    Command,
    DatastoreAction,
    Status,
)
from mcubridge.protocol.structures import (
    DatastoreGetPacket,
    DatastoreGetResponsePacket,
    DatastorePutPacket,
)

from ..config.const import MQTT_EXPIRY_DATASTORE
from ..config.settings import RuntimeConfig
from ..protocol.topics import Topic, split_topic_segments, topic_path
from ..state.context import RuntimeState
from .base import BridgeContext

logger = logging.getLogger("mcubridge.datastore")


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
        try:
            packet = DatastorePutPacket.decode(payload)
        except (ConstructError, ValueError):
            logger.warning(
                "Malformed DATASTORE_PUT payload: %s",
                payload.hex(),
            )
            return False

        key = packet.key
        value_bytes = packet.value
        value = value_bytes.decode("utf-8", errors="ignore")

        self.state.datastore[key] = value
        await self._publish_value(key, value_bytes)
        return True

    async def handle_get_request(self, payload: bytes) -> bool:
        """Handle CMD_DATASTORE_GET initiated by the MCU."""
        try:
            packet = DatastoreGetPacket.decode(payload)
        except (ConstructError, ValueError):
            logger.warning(
                "Malformed DATASTORE_GET payload: %s",
                payload.hex() if payload else "(empty)",
            )
            await self.ctx.send_frame(
                Status.MALFORMED.value,
                b"data_get_malformed",
            )
            return False

        key = packet.key
        val: Any = self.state.datastore.get(key, "")

        # [SIL-2] Type-safe value coercion
        value_bytes = val.encode("utf-8") if isinstance(val, str) else bytes(val)

        if len(value_bytes) > 255:
            logger.warning(
                "Datastore value truncated for key %s (%d bytes)",
                key,
                len(value_bytes),
            )
            value_bytes = value_bytes[:255]

        # [SIL-2] Use structured response packet
        response_payload = DatastoreGetResponsePacket(value=value_bytes).encode()

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
        inbound: Message | None = None,
    ) -> None:
        parts = remainder.copy()
        is_request = identifier == DatastoreAction.GET and bool(parts) and parts[-1] == "request"
        if is_request:
            parts.pop()

        key = "/".join(parts)
        if not key:
            logger.debug("Ignoring datastore action '%s' without key", identifier)
            return

        match identifier:
            case DatastoreAction.PUT:
                await self._handle_mqtt_put(key, payload_str, inbound)
            case DatastoreAction.GET:
                await self._handle_mqtt_get(key, is_request, inbound)
            case _:
                logger.debug("Unknown datastore action '%s'", identifier)

    async def _handle_mqtt_put(
        self,
        key: str,
        value_text: str,
        inbound: Message | None,
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
        inbound: Message | None,
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
                logger.debug("Datastore GET for '%s' has no cached value", key)
            return

        # [SIL-2] Handle potential type drift during testing/injection
        val_to_check: Any = cached_value
        val_bytes = val_to_check.encode("utf-8") if isinstance(val_to_check, str) else val_to_check

        # Ignore echoes: if it's not an explicit /request and it has a payload,
        # it is an echo of a published value, so we do not republish.
        if not is_request and inbound and inbound.payload:
            return

        await self._publish_value(
            key,
            val_bytes,
            reply_context=inbound,
        )

    async def _publish_value(
        self,
        key: str,
        value: bytes,
        *,
        reply_context: Message | None = None,
        error_reason: str | None = None,
    ) -> None:
        key_segments = split_topic_segments(key)
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.DATASTORE,
            DatastoreAction.GET,
            *key_segments,
        )
        properties: list[tuple[str, str]] = [("bridge-datastore-key", key)]
        if error_reason:
            properties.append(("bridge-error", error_reason))

        await self.ctx.publish(
            topic=topic_name,
            payload=value,
            expiry=MQTT_EXPIRY_DATASTORE,
            content_type="text/plain; charset=utf-8",
            properties=tuple(properties),
            reply_to=reply_context,
        )


__all__ = ["DatastoreComponent"]
