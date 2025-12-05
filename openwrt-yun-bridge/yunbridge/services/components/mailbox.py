"""Mailbox component encapsulating MCU/Linux mailbox flows."""
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from typing import Optional

from yunbridge.rpc.protocol import Command, Status, MAX_PAYLOAD_SIZE

from ...common import encode_status_reason, pack_u16, unpack_u16
from ...mqtt import InboundMessage, PublishableMessage
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
            message_id = unpack_u16(payload)

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

        msg_len = unpack_u16(payload)
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

        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "incoming",
        )
        await self.ctx.enqueue_mqtt(
            PublishableMessage(topic_name=topic, payload=data)
        )

        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=mailbox_incoming_available_topic(
                    self.state.mqtt_topic_prefix
                ),
                payload=str(
                    len(self.state.mailbox_incoming_queue)
                ).encode("utf-8"),
            )
        )
        return True

    async def handle_available(self, _: bytes) -> None:
        queue_len = len(self.state.mailbox_queue) & 0xFF
        count_payload = bytes((queue_len & 0xFF,))
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

        response_payload = pack_u16(msg_len) + message_payload
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
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        match action:
            case "write":
                await self._handle_mqtt_write(payload)
            case "read":
                await self._handle_mqtt_read(inbound)
            case _:
                logger.debug("Ignoring mailbox action '%s'", action)

    async def handle_mqtt_write(
        self,
        payload: bytes,
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        await self._handle_mqtt_write(payload)

    async def handle_mqtt_read(
        self,
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        await self._handle_mqtt_read(inbound)

    async def _handle_mqtt_write(
        self,
        payload: bytes,
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
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self._publish_outgoing_available)

    async def _handle_mqtt_read(
        self,
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "incoming",
        )

        if self.state.mailbox_incoming_queue:
            async with AsyncExitStack() as stack:
                stack.push_async_callback(self._publish_incoming_available)
                message_payload = self.state.pop_mailbox_incoming()
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
                return

        message_payload = self.state.pop_mailbox_message()
        if message_payload is None:
            return

        message = PublishableMessage(
            topic_name=topic,
            payload=message_payload,
        )
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self._publish_outgoing_available)
            await self.ctx.enqueue_mqtt(
                message,
                reply_context=inbound,
            )

    async def _publish_incoming_available(self) -> None:
        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=mailbox_incoming_available_topic(
                    self.state.mqtt_topic_prefix
                ),
                payload=str(
                    len(self.state.mailbox_incoming_queue)
                ).encode("utf-8"),
            )
        )

    async def _publish_outgoing_available(self) -> None:
        await self.ctx.enqueue_mqtt(
            PublishableMessage(
                topic_name=mailbox_outgoing_available_topic(
                    self.state.mqtt_topic_prefix
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )


__all__ = ["MailboxComponent"]
