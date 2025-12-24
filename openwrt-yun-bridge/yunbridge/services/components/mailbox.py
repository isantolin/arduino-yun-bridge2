"""Mailbox component encapsulating MCU/Linux mailbox flows."""

from __future__ import annotations

import json
import logging
import struct

from aiomqtt.message import Message as MQTTMessage
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import (
    UINT8_MASK,
    Action,
    Command,
    Status,
)

from ...common import encode_status_reason
from ...mqtt.messages import QueuedPublish
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
from ...protocol.topics import (
    Topic,
    mailbox_incoming_available_topic,
    mailbox_outgoing_available_topic,
    topic_path,
)
from .base import BridgeContext

logger = logging.getLogger("yunbridge.mailbox")


class MailboxComponent:
    """Handle mailbox interactions between MCU and Linux."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx

    async def handle_processed(self, payload: bytes) -> bool:
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "processed",
        )
        message_id: int | None = None
        if len(payload) >= 2:
            message_id = struct.unpack(">H", payload[:2])[0]

        if message_id is not None:
            body = json.dumps({"message_id": message_id}).encode("utf-8")
        else:
            body = payload

        await self.ctx.enqueue_mqtt(QueuedPublish(topic_name=topic_name, payload=body))
        return True

    async def handle_push(self, payload: bytes) -> bool:
        if len(payload) < 2:
            logger.warning(
                "Malformed MAILBOX_PUSH payload: %s",
                payload.hex(),
            )
            return False

        msg_len = struct.unpack(">H", payload[:2])[0]
        data = payload[2 : 2 + msg_len]
        if len(data) != msg_len:
            logger.warning(
                "MAILBOX_PUSH length mismatch. Expected %d bytes, got %d.",
                msg_len,
                len(data),
            )
            return False

        stored = self.state.enqueue_mailbox_incoming(data, logger)
        if not stored:
            logger.error(
                "Dropping incoming mailbox message (%d bytes) due to " "queue limits.",
                len(data),
            )
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason("mailbox_incoming_overflow"),
            )
            return False

        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "incoming",
        )
        await self.ctx.enqueue_mqtt(QueuedPublish(topic_name=topic, payload=data))

        await self.ctx.enqueue_mqtt(
            QueuedPublish(
                topic_name=mailbox_incoming_available_topic(
                    self.state.mqtt_topic_prefix
                ),
                payload=str(len(self.state.mailbox_incoming_queue)).encode("utf-8"),
            )
        )
        return True

    async def handle_available(self, payload: bytes) -> bool:
        """Handle CMD_MAILBOX_AVAILABLE."""
        # Just return the count of messages in queue
        queue_len = len(self.state.mailbox_queue) & UINT8_MASK
        count_payload = bytes((queue_len & UINT8_MASK,))
        await self.ctx.send_frame(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            count_payload,
        )
        return True

    async def handle_read(self, _: bytes) -> bool:
        original_payload = self.state.pop_mailbox_message()
        message_payload = original_payload if original_payload is not None else b""

        msg_len = len(message_payload)
        if msg_len > protocol.MAX_PAYLOAD_SIZE - 2:
            logger.warning(
                "Mailbox message too long (%d bytes), truncating.",
                msg_len,
            )
            message_payload = message_payload[: protocol.MAX_PAYLOAD_SIZE - 2]
            msg_len = len(message_payload)

        response_payload = (
            struct.pack(protocol.UINT16_FORMAT, msg_len) + message_payload
        )
        send_ok = await self.ctx.send_frame(
            Command.CMD_MAILBOX_READ_RESP.value,
            response_payload,
        )

        if not send_ok:
            if original_payload is not None:
                self.state.requeue_mailbox_message_front(original_payload)
            return False

        await self.ctx.enqueue_mqtt(
            QueuedPublish(
                topic_name=mailbox_outgoing_available_topic(
                    self.state.mqtt_topic_prefix
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )
        return True

    async def handle_mqtt(
        self,
        action: str,
        payload: bytes,
        inbound: MQTTMessage | None = None,
    ) -> None:
        match action:
            case Action.MAILBOX_WRITE:
                await self._handle_mqtt_write(payload, inbound)
            case Action.MAILBOX_READ:
                await self._handle_mqtt_read(inbound)
            case _:
                logger.debug("Ignoring mailbox action '%s'", action)

    async def handle_mqtt_write(
        self,
        payload: bytes,
        inbound: MQTTMessage | None = None,
    ) -> None:
        await self._handle_mqtt_write(payload, inbound)

    async def handle_mqtt_read(
        self,
        inbound: MQTTMessage | None = None,
    ) -> None:
        await self._handle_mqtt_read(inbound)

    async def _handle_mqtt_write(
        self,
        payload: bytes,
        inbound: MQTTMessage | None = None,
    ) -> None:
        if not self.state.enqueue_mailbox_message(payload, logger):
            await self._handle_outgoing_overflow(len(payload), inbound)
            return
        queue_len = len(self.state.mailbox_queue)
        logger.info(
            "Added message to mailbox queue. Size=%d",
            queue_len,
            extra={
                "queue_len": queue_len,
                "queue_limit": self.state.mailbox_queue_limit,
                "queue_bytes_used": self.state.mailbox_queue_bytes,
            },
        )
        await self._publish_outgoing_available()

    async def _handle_mqtt_read(
        self,
        inbound: MQTTMessage | None = None,
    ) -> None:
        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "incoming",
        )

        if self.state.mailbox_incoming_queue:
            message_payload = self.state.pop_mailbox_incoming()
            if message_payload is None:
                await self._publish_incoming_available()
                return

            message = QueuedPublish(
                topic_name=topic,
                payload=message_payload,
            )

            try:
                await self.ctx.enqueue_mqtt(
                    message,
                    reply_context=inbound,
                )
            finally:
                await self._publish_incoming_available()
            return

        message_payload = self.state.pop_mailbox_message()
        if message_payload is None:
            return

        message = QueuedPublish(
            topic_name=topic,
            payload=message_payload,
        )
        try:
            await self.ctx.enqueue_mqtt(
                message,
                reply_context=inbound,
            )
        finally:
            await self._publish_outgoing_available()

    async def _handle_outgoing_overflow(
        self,
        payload_size: int,
        inbound: MQTTMessage | None,
    ) -> None:
        queue_len = len(self.state.mailbox_queue)
        logger.error(
            "Mailbox outgoing queue full; rejecting MQTT payload (%d bytes)",
            payload_size,
            extra={
                "queue_len": queue_len,
                "queue_limit": self.state.mailbox_queue_limit,
                "queue_bytes_limit": self.state.mailbox_queue_bytes_limit,
                "queue_bytes_used": self.state.mailbox_queue_bytes,
                "payload_bytes": payload_size,
            },
        )
        await self.ctx.send_frame(
            Status.ERROR.value,
            encode_status_reason("mailbox_outgoing_overflow"),
        )
        await self._publish_outgoing_available()
        overflow_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "errors",
        )
        body = json.dumps(
            {
                "event": "write_overflow",
                "reason": "mailbox_outgoing_overflow",
                "queue_size": queue_len,
                "queue_limit": self.state.mailbox_queue_limit,
                "queue_bytes_limit": self.state.mailbox_queue_bytes_limit,
                "payload_bytes": payload_size,
                "overflow_events": self.state.mailbox_outgoing_overflow_events,
            }
        ).encode("utf-8")
        properties: tuple[tuple[str, str], ...]
        if inbound is not None:
            properties = (("bridge-error", Topic.MAILBOX.value),)
        else:
            properties = ()
        message = QueuedPublish(
            topic_name=overflow_topic,
            payload=body,
            content_type="application/json",
            user_properties=properties,
        )
        await self.ctx.enqueue_mqtt(message, reply_context=inbound)

    async def _publish_incoming_available(self) -> None:
        await self._publish_queue_depth(
            topic_name=mailbox_incoming_available_topic(self.state.mqtt_topic_prefix),
            length=len(self.state.mailbox_incoming_queue),
        )

    async def _publish_outgoing_available(self) -> None:
        await self._publish_queue_depth(
            topic_name=mailbox_outgoing_available_topic(self.state.mqtt_topic_prefix),
            length=len(self.state.mailbox_queue),
        )

    async def _publish_queue_depth(
        self,
        *,
        topic_name: str,
        length: int,
    ) -> None:
        await self.ctx.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic_name,
                payload=str(length).encode("utf-8"),
            )
        )


__all__ = ["MailboxComponent"]
