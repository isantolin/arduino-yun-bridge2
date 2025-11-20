"""Mailbox component encapsulating MCU/Linux mailbox flows."""
from __future__ import annotations

import json
import logging
import struct
from typing import Optional

from yunbridge.rpc.protocol import Command, Status, MAX_PAYLOAD_SIZE

from ...common import encode_status_reason
from ...const import (
    TOPIC_MAILBOX,
    TOPIC_MAILBOX_INCOMING_AVAILABLE,
    TOPIC_MAILBOX_OUTGOING_AVAILABLE,
)
from ...mqtt import InboundMessage, PublishableMessage
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
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
        topic_name = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/processed"
        )
        message_id: int | None = None
        if len(payload) >= 2:
            (message_id,) = struct.unpack(">H", payload[:2])

        if message_id is not None:
            body = json.dumps({"message_id": message_id}).encode("utf-8")
        else:
            body = payload

        await self.ctx.enqueue_mqtt(
            PublishableMessage(topic_name=topic_name, payload=body)
        )
        return True

    async def handle_push(self, payload: bytes) -> bool:
        if len(payload) < 2:
            logger.warning(
                "Malformed MAILBOX_PUSH payload: %s",
                payload.hex(),
            )
            return False

        (msg_len,) = struct.unpack(">H", payload[:2])
        data = payload[2:2 + msg_len]
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
                "Dropping incoming mailbox message (%d bytes) due to "
                "queue limits.",
                len(data),
            )
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason("mailbox_incoming_overflow"),
            )
            return False

        topic = self.state.mailbox_incoming_topic or (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/incoming"
        )
        await self.ctx.enqueue_mqtt(
            PublishableMessage(topic_name=topic, payload=data)
        )

        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_INCOMING_AVAILABLE}"
                ),
                payload=str(
                    len(self.state.mailbox_incoming_queue)
                ).encode("utf-8"),
            )
        )
        return True

    async def handle_available(self, _: bytes) -> None:
        queue_len = len(self.state.mailbox_queue) & 0xFF
        count_payload = struct.pack(">B", queue_len)
        await self.ctx.send_frame(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            count_payload,
        )

    async def handle_read(self, _: bytes) -> bool:
        original_payload = self.state.pop_mailbox_message()
        message_payload = (
            original_payload if original_payload is not None else b""
        )

        msg_len = len(message_payload)
        if msg_len > MAX_PAYLOAD_SIZE - 2:
            logger.warning(
                "Mailbox message too long (%d bytes), truncating.",
                msg_len,
            )
            message_payload = message_payload[: MAX_PAYLOAD_SIZE - 2]
            msg_len = len(message_payload)

        response_payload = struct.pack(">H", msg_len) + message_payload
        send_ok = await self.ctx.send_frame(
            Command.CMD_MAILBOX_READ_RESP.value,
            response_payload,
        )

        if not send_ok:
            if original_payload is not None:
                self.state.requeue_mailbox_message_front(original_payload)
            return False

        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_OUTGOING_AVAILABLE}"
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )
        return True

    async def handle_mqtt_write(
        self,
        payload: bytes,
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        if not self.state.enqueue_mailbox_message(payload, logger):
            logger.error(
                "Failed to enqueue MQTT mailbox payload (%d bytes); "
                "queue full.",
                len(payload),
            )
            return
        logger.info(
            "Added message to mailbox queue. Size=%d",
            len(self.state.mailbox_queue),
        )
        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_OUTGOING_AVAILABLE}"
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )

    async def handle_mqtt_read(
        self,
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        topic = self.state.mailbox_incoming_topic or (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/incoming"
        )

        if self.state.mailbox_incoming_queue:
            message_payload = self.state.pop_mailbox_incoming()
            if message_payload is None:
                await self._publish_incoming_available()
                return

            message = PublishableMessage(
                topic_name=topic,
                payload=message_payload,
            )

            await self.ctx.enqueue_mqtt(
                message,
                reply_context=inbound,
            )

            await self._publish_incoming_available()
            return

        message_payload = self.state.pop_mailbox_message()
        if message_payload is None:
            return

        message = PublishableMessage(
            topic_name=topic,
            payload=message_payload,
        )
        await self.ctx.enqueue_mqtt(
            message,
            reply_context=inbound,
        )

        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_OUTGOING_AVAILABLE}"
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )

    async def _publish_incoming_available(self) -> None:
        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_INCOMING_AVAILABLE}"
                ),
                payload=str(
                    len(self.state.mailbox_incoming_queue)
                ).encode("utf-8"),
            )
        )


__all__ = ["MailboxComponent"]
