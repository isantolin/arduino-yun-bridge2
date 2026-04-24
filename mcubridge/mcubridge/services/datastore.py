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
        mqtt_flow: MqttTransport,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.mqtt_flow = mqtt_flow

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

        send_ok = await self.serial_flow.send(
            Command.CMD_DATASTORE_GET_RESP.value,
            response_payload,
        )
        if send_ok:
            await self._publish_datastore_value(key, value_bytes)
        return send_ok

    async def handle_mqtt(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        identifier = route.identifier
        remainder = list(route.remainder)
        payload = msgspec.convert(inbound.payload, bytes)
        payload_str = payload.decode("utf-8", errors="ignore")

        is_request = (
            identifier == DatastoreAction.GET
            and bool(remainder)
            and remainder[-1] == "request"
        )
        parts = remainder[:-1] if is_request else remainder

        key = "/".join(parts)
        if not key:
            logger.debug("Ignoring datastore action '%s' without key", identifier)
            return True

        match identifier:
            case DatastoreAction.PUT:
                await self._handle_mqtt_put(key, payload_str, inbound)
            case DatastoreAction.GET:
                await self._handle_mqtt_get(key, is_request, inbound)
            case _:
                logger.debug("Unknown datastore action '%s'", identifier)
        return True

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
        await self._publish_datastore_value(
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
                await self._publish_datastore_value(
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
        val_bytes = (
            val_to_check.encode("utf-8")
            if isinstance(val_to_check, str)
            else bytes(val_to_check)
        )

        # Ignore echoes: if it's not an explicit /request and it has a payload,
        # it is an echo of a published value, so we do not republish.
        if not is_request and inbound and inbound.payload:
            return

        await self._publish_datastore_value(
            key,
            val_bytes,
            reply_context=inbound,
        )

    async def _publish_datastore_value(
        self,
        key: str,
        value: bytes,
        *,
        reply_context: Message | None = None,
        error_reason: str | None = None,
    ) -> None:
        key_segments = tuple(filter(None, key.split("/")))
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.DATASTORE,
            DatastoreAction.GET,
            *key_segments,
        )
        properties: list[tuple[str, str]] = [("bridge-datastore-key", key)]
        if error_reason:
            properties.append(("bridge-error", error_reason))

        # Direct call to mqtt_flow.publish (Zero Wrapper)
        props_tuple = tuple(properties)
        await self.mqtt_flow.publish(
            topic=topic_name,
            payload=value,
            expiry=MQTT_EXPIRY_DATASTORE,
            reply_to=None,
            properties=props_tuple,
        )
        if reply_context is not None:
            await self.mqtt_flow.publish(
                topic=topic_name,
                payload=value,
                expiry=MQTT_EXPIRY_DATASTORE,
                reply_to=reply_context,
                properties=props_tuple,
            )


__all__ = ["DatastoreComponent"]
