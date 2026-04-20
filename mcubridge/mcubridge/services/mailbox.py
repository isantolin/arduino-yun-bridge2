"""Mailbox component encapsulating MCU/Linux mailbox flows."""

from __future__ import annotations

import structlog

import msgspec
from aiomqtt.message import Message

from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    Command,
    MailboxAction,
    Status,
)
from mcubridge.protocol.structures import (
    AckPacket,
    MailboxAvailableResponsePacket,
    MailboxProcessedPacket,
    MailboxPushPacket,
    MailboxReadResponsePacket,
    TopicRoute,
)

from ..protocol.topics import (
    Topic,
    topic_path,
)
from .base import BaseComponent

logger = structlog.get_logger("mcubridge.mailbox")
_msgpack_enc = msgspec.msgpack.Encoder()


class MailboxComponent(BaseComponent):
    """Handle mailbox interactions between MCU and Linux."""

    async def handle_processed(self, seq_id: int, payload: bytes) -> bool:
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            MailboxAction.PROCESSED,
        )
        message_id: int | None = None
        if len(payload) >= 2:
            try:
                packet = MailboxProcessedPacket.decode(payload, Command.CMD_MAILBOX_PROCESSED)
                message_id = packet.message_id
            except ValueError as exc:
                logger.warning("MCU > Malformed Mailbox processed payload: %s", exc)

        if message_id is not None:
            body = _msgpack_enc.encode({"message_id": message_id})
        else:
            body = payload

        await self.ctx.mqtt_flow.publish(topic=topic_name, payload=body)
        return True

    async def handle_push(self, seq_id: int, payload: bytes) -> bool:
        try:
            packet = MailboxPushPacket.decode(payload, Command.CMD_MAILBOX_PUSH)
        except ValueError:
            logger.warning("Malformed MailboxPushPacket payload: %s", payload.hex())
            return False

        data = packet.data

        stored = self.state.mailbox_incoming_queue.append(data).success
        if not stored:
            logger.error(
                "Dropping incoming mailbox message (%d bytes) due to queue limits.",
                len(data),
            )
            reason = protocol.STATUS_REASON_MAILBOX_INCOMING_OVERFLOW
            await self.ctx.serial_flow.send(
                Status.ERROR.value,
                reason.encode("utf-8", errors="ignore")[: protocol.MAX_PAYLOAD_SIZE],
            )
            return False

        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            MailboxAction.INCOMING,
        )
        await self.ctx.mqtt_flow.publish(topic=topic, payload=data)

        await self.ctx.mqtt_flow.publish(
            topic=topic_path(self.state.mqtt_topic_prefix, Topic.MAILBOX, "incoming_available"),
            payload=str(len(self.state.mailbox_incoming_queue)).encode("utf-8"),
        )
        return True

    async def handle_available(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_MAILBOX_AVAILABLE."""
        if payload:
            await self.ctx.serial_flow.send(
                Status.MALFORMED.value,
                AckPacket(command_id=Command.CMD_MAILBOX_AVAILABLE.value).encode(),
            )
            return False

        queue_len = len(self.state.mailbox_queue)
        response = MailboxAvailableResponsePacket(count=queue_len).encode()

        await self.ctx.serial_flow.send(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            response,
        )
        return True

    async def handle_read(self, seq_id: int, _: bytes) -> bool:
        original_payload = self.state.mailbox_queue.popleft()
        message_payload: bytes = original_payload if original_payload is not None else b""

        max_allowed = protocol.MAX_PAYLOAD_SIZE - 3
        if len(message_payload) > max_allowed:
            logger.warning("Mailbox message too long (%d bytes), truncating.", len(message_payload))
            message_payload = message_payload[:max_allowed]

        response_payload = MailboxReadResponsePacket(content=message_payload).encode()

        send_ok = await self.ctx.serial_flow.send(
            Command.CMD_MAILBOX_READ_RESP.value,
            response_payload,
        )

        if not send_ok:
            if original_payload is not None:
                self.state.mailbox_queue.appendleft(original_payload)
            return False

        await self._publish_available("outgoing_available", len(self.state.mailbox_queue))
        return True

    async def handle_mqtt(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        payload = msgspec.convert(inbound.payload, bytes)
        action = route.identifier
        match action:
            case MailboxAction.WRITE:
                await self._handle_mqtt_write(payload, inbound)
            case MailboxAction.READ:
                await self._handle_mqtt_read(inbound)
            case _:
                logger.debug("Ignoring mailbox action '%s'", action)
        return True

    async def _handle_mqtt_write(
        self,
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        if not self.state.mailbox_queue.append(payload).success:
            await self._handle_outgoing_overflow(len(payload), inbound)
            return
        queue_len = len(self.state.mailbox_queue)
        logger.info(
            "Added message to mailbox queue. Size=%d",
            queue_len,
            queue_len=queue_len,
            queue_limit=self.state.mailbox_queue_limit,
            queue_bytes_used=self.state.mailbox_queue_bytes,
        )
        await self._publish_available("outgoing_available", queue_len)

    async def _handle_mqtt_read(
        self,
        inbound: Message | None = None,
    ) -> None:
        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            MailboxAction.INCOMING,
        )

        if self.state.mailbox_incoming_queue:
            message_payload = self.state.mailbox_incoming_queue.popleft()
            if message_payload is None:
                await self._publish_available("incoming_available", 0)
                return

            try:
                await self.ctx.mqtt_flow.publish(
                    topic=topic,
                    payload=message_payload,
                    reply_to=inbound,
                )
            finally:
                await self._publish_available("incoming_available", len(self.state.mailbox_incoming_queue))
            return

        message_payload = self.state.mailbox_queue.popleft()
        if message_payload is None:
            return

        try:
            await self.ctx.mqtt_flow.publish(
                topic=topic,
                payload=message_payload,
                reply_to=inbound,
            )
        finally:
            await self._publish_available("outgoing_available", len(self.state.mailbox_queue))

    async def _handle_outgoing_overflow(
        self,
        payload_size: int,
        inbound: Message | None,
    ) -> None:
        queue_len = len(self.state.mailbox_queue)
        logger.error(
            "Mailbox outgoing queue full; rejecting MQTT payload (%d bytes)",
            payload_size,
            queue_len=queue_len,
            queue_limit=self.state.mailbox_queue_limit,
            queue_bytes_limit=self.state.mailbox_queue_bytes_limit,
            queue_bytes_used=self.state.mailbox_queue_bytes,
            payload_bytes=payload_size,
        )
        reason = protocol.STATUS_REASON_MAILBOX_OUTGOING_OVERFLOW
        await self.ctx.serial_flow.send(
            Status.ERROR.value,
            reason.encode("utf-8", errors="ignore")[: protocol.MAX_PAYLOAD_SIZE],
        )
        await self._publish_available("outgoing_available", queue_len)
        overflow_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            MailboxAction.ERRORS,
        )

        body = _msgpack_enc.encode(
            {
                "event": "write_overflow",
                "reason": protocol.STATUS_REASON_MAILBOX_OUTGOING_OVERFLOW,
                "queue_size": queue_len,
                "queue_limit": self.state.mailbox_queue_limit,
                "queue_bytes_limit": self.state.mailbox_queue_bytes_limit,
                "payload_bytes": payload_size,
                "overflow_events": self.state.mailbox_outgoing_overflow_events,
            }
        )

        properties: tuple[tuple[str, str], ...]
        if inbound is not None:
            properties = (("bridge-error", Topic.MAILBOX.value),)
        else:
            properties = ()

        await self.ctx.mqtt_flow.publish(
            topic=overflow_topic,
            payload=body,
            content_type="application/msgpack",
            properties=properties,
            reply_to=inbound,
        )

    async def _publish_available(
        self,
        suffix: str,
        count: int,
    ) -> None:
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            suffix,
        )
        await self.ctx.mqtt_flow.publish(
            topic=topic_name,
            payload=str(count).encode("utf-8"),
        )


__all__ = ["MailboxComponent"]
