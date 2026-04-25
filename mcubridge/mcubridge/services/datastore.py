"""Datastore component for MCU/Linux interactions."""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

import msgspec
from aiomqtt.message import Message

from mcubridge.protocol.protocol import (
    Command,
    DatastoreAction,
    Status,
)
from mcubridge.protocol.structures import (
    DatastoreGetPacket,
    DatastoreGetResponsePacket,
    DatastorePutPacket,
    TopicRoute,
)

from ..config.const import MQTT_EXPIRY_DATASTORE
from ..mqtt import atomic_publish
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
    from ..transport.mqtt import MqttTransport
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.datastore")


class DatastoreComponent:
    """Encapsulate datastore behaviour for BridgeService. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        mqtt_flow: Any | None = None,  # Kept for compatibility
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow

    async def handle_put(self, seq_id: int, payload: bytes) -> bool:
        """Process CMD_DATASTORE_PUT received from the MCU."""
        try:
            # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
            packet = msgspec.msgpack.decode(payload, type=DatastorePutPacket)
        except (ValueError, msgspec.DecodeError):
            logger.warning("Malformed DatastorePutPacket payload: %s", payload.hex())
            return False

        key = packet.key
        value_bytes = packet.value
        value = value_bytes.decode("utf-8", errors="ignore")

        self.state.datastore[key] = value
        await self._publish_datastore_value(key, value_bytes)
        return True

    async def handle_get_request(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_DATASTORE_GET initiated by the MCU."""
        try:
            # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
            packet = msgspec.msgpack.decode(payload, type=DatastoreGetPacket)
        except (ValueError, msgspec.DecodeError):
            logger.warning(
                "Malformed DATASTORE_GET payload: %s",
                payload.hex() if payload else "(empty)",
            )
            await self.serial_flow.send(
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

        # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
        response_payload = msgspec.msgpack.encode(
            DatastoreGetResponsePacket(value=value_bytes)
        )

        await self.serial_flow.send(
            Command.CMD_DATASTORE_GET_RESP.value,
            response_payload,
        )
        return True

    async def _publish_datastore_value(self, key: str, value: bytes) -> None:
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.DATASTORE,
            key,
            "value",
        )
        # [SIL-2] Direct publication (Eradicates MqttTransport wrapper)
        await atomic_publish(
            self.state,
            topic=topic,
            payload=value,
            expiry=MQTT_EXPIRY_DATASTORE,
        )

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        if not route.segments:
            return False

        action = route.action
        # [SIL-2] Extract key by joining all segments except the first one (action)
        key = "/".join(route.segments[1:]) if len(route.segments) > 1 else ""

        if action == DatastoreAction.PUT.value:
            if not key:
                return False
            
            payload = msgspec.convert(inbound.payload, bytes)
            self.state.datastore[key] = payload.decode("utf-8", errors="ignore")

            # Forward to MCU
            frame_payload = msgspec.msgpack.encode(
                DatastorePutPacket(key=key, value=payload)
            )
            await self.serial_flow.send(
                Command.CMD_DATASTORE_PUT.value,
                frame_payload,
            )
            return True

        if action == DatastoreAction.GET.value:
            if not key:
                return False

            val: Any = self.state.datastore.get(key, "")
            value_bytes = val.encode("utf-8") if isinstance(val, str) else bytes(val)

            topic = topic_path(
                self.state.mqtt_topic_prefix,
                Topic.DATASTORE,
                key,
                "value",
            )
            # Response to GET request
            await atomic_publish(
                self.state,
                topic=topic,
                payload=value_bytes,
                expiry=MQTT_EXPIRY_DATASTORE,
                reply_to=inbound,
            )
            return True

        return False


__all__ = ["DatastoreComponent"]
