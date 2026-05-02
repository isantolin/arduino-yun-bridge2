"""Datastore component for MCU/Linux interactions."""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

import msgspec
from aiomqtt.message import Message

from ..protocol.protocol import (
    Command,
    Status,
)
from ..protocol.structures import (
    DatastoreGetPacket,
    DatastoreGetResponsePacket,
    DatastorePutPacket,
    QueuedPublish,
    TopicRoute,
)

from ..config.const import MQTT_EXPIRY_DATASTORE
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.datastore")


class DatastoreComponent:
    """Persistent and transient state management. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        enqueue_mqtt: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.enqueue_mqtt = enqueue_mqtt

    async def handle_put(self, seq_id: int, payload: bytes) -> bool:
        """Process CMD_DATASTORE_PUT received from the MCU."""
        try:
            # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
            packet = msgspec.msgpack.decode(payload, type=DatastorePutPacket)
        except (ValueError, msgspec.DecodeError):
            logger.warning("Malformed DatastorePutPacket payload: %s", payload.hex())
            return False

        key = packet.key
        value = packet.value

        # Atomic updates are guaranteed by the GIL in the context of dict mutation.
        self.state.datastore[key] = value

        # Propagate change to MQTT if configured
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.DATASTORE,
            "value",
            *key.split("/"),
        )
        props_tuple = (("bridge-datastore-key", key),)

        try:
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_name=topic_name,
                    payload=value,
                    message_expiry_interval=MQTT_EXPIRY_DATASTORE,
                    user_properties=props_tuple,
                )
            )
        except (RuntimeError, ValueError) as e:
            logger.error("Failed to enqueue datastore update for MQTT: %s", e)

        await self.serial_flow.send(Status.OK.value, b"")
        return True

    async def handle_get_request(self, seq_id: int, payload: bytes) -> bool:
        """Process CMD_DATASTORE_GET received from the MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=DatastoreGetPacket)
        except (ValueError, msgspec.DecodeError):
            logger.warning("Malformed DatastoreGetPacket payload: %s", payload.hex())
            return False

        key = packet.key
        value = self.state.datastore.get(key)

        if value is None:
            await self.serial_flow.send(Status.ERROR.value, b"")
            return True

        # Send value back to MCU
        response = DatastoreGetResponsePacket(value=value)
        encoded_resp = msgspec.msgpack.encode(response)
        return await self.serial_flow.send(
            Command.CMD_DATASTORE_GET_RESP.value, encoded_resp
        )

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Process inbound MQTT requests for datastore operations."""
        action = route.identifier
        key = "/".join(route.remainder)

        if not key:
            await self._publish_datastore_value(
                "",
                b"error:missing_key",
                error_reason="missing_key",
                reply_context=inbound,
            )
            return True

        match action:
            case "get":
                value = self.state.datastore.get(key)
                if value is not None:
                    await self._publish_datastore_value(
                        key, value, reply_context=inbound
                    )
                else:
                    await self._publish_datastore_value(
                        key,
                        b"error:not_found",
                        error_reason="not_found",
                        reply_context=inbound,
                    )
                return True

            case "put":
                try:
                    value = msgspec.convert(inbound.payload, bytes)
                    self.state.datastore[key] = value

                    # Notify MCU about the external change if needed
                    # [SIL-2] Async dispatch to serial flow controller
                    packet = DatastorePutPacket(key=key, value=value)
                    await self.serial_flow.send(
                        Command.CMD_DATASTORE_PUT.value, msgspec.msgpack.encode(packet)
                    )
                    return True
                except (ValueError, msgspec.MsgspecError) as e:
                    logger.warning("Malformed MQTT datastore put: %s", e)
                    await self._publish_datastore_value(
                        key,
                        b"error:malformed_payload",
                        error_reason="malformed_payload",
                        reply_context=inbound,
                    )
                    return True
            case _:
                return False

    async def _publish_datastore_value(
        self,
        key: str,
        value: bytes,
        *,
        error_reason: str | None = None,
        reply_context: Message | None = None,
    ) -> None:
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.DATASTORE,
            "get",
            "resp",
            *key.split("/"),
        )
        props_tuple = (
            (("bridge-error", error_reason),)
            if error_reason
            else (("bridge-datastore-key", key),)
        )

        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic_name,
                payload=value,
                message_expiry_interval=MQTT_EXPIRY_DATASTORE,
                user_properties=props_tuple,
            ),
            reply_context=reply_context,
        )


__all__ = ["DatastoreComponent"]
