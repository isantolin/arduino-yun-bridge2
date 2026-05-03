"""Mailbox component encapsulating MCU/Linux mailbox flows."""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

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
    QueuedPublish,
    TopicRoute,
)

from ..protocol.topics import (
    Topic,
    topic_path,
)

if TYPE_CHECKING:
    from ..transport.mqtt import MqttTransport
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.mailbox")


class MailboxComponent:
    """Handle mailbox interactions between MCU and Linux. [SIL-2]"""

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

    async def handle_processed(self, seq_id: int, payload: bytes) -> bool:
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            MailboxAction.PROCESSED,
        )
        message_id: int | None = None
        if len(payload) >= 2:
            try:
                # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
                packet = msgspec.msgpack.decode(payload, type=MailboxProcessedPacket)
                message_id = packet.message_id
            except ValueError as exc:
                logger.warning("MCU > Malformed Mailbox processed payload: %s", exc)

        if message_id is not None:
            # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
            body = msgspec.msgpack.encode({"message_id": message_id})
        else:
            body = payload

        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(topic_name=topic_name, payload=body)
        )
        return True

    async def handle_push(self, seq_id: int, payload: bytes) -> bool:
        try:
            # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
            packet = msgspec.msgpack.decode(payload, type=MailboxPushPacket)
        except ValueError:
            logger.warning("Malformed MailboxPushPacket payload: %s", payload.hex())
            return False

        data = packet.data

        queue = self.state.mailbox_incoming_queue
        limit = self.state.mailbox_queue_limit

        if limit > 0 and len(queue) >= limit:
            # [SIL-2] Manual FIFO eviction to maintain deterministic queue size
            queue.popleft()
            self.state.mailbox_incoming_dropped_messages += 1
            self.state.mailbox_incoming_overflow_events += 1

        queue.append(data)

        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            MailboxAction.INCOMING,
        )
        await self.mqtt_flow.enqueue_mqtt(QueuedPublish(topic_name=topic, payload=data))

        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic_path(
                    self.state.mqtt_topic_prefix, Topic.MAILBOX, "incoming_available"
                ),
                payload=str(len(self.state.mailbox_incoming_queue)).encode("utf-8"),
            )
        )
        return True

    async def handle_available(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_MAILBOX_AVAILABLE."""
        if payload:
            await self.serial_flow.send(
                Status.MALFORMED.value,
                # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
                msgspec.msgpack.encode(
                    AckPacket(command_id=Command.CMD_MAILBOX_AVAILABLE.value)
                ),
            )
            return False

        queue_len = len(self.state.mailbox_queue)
        # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
        response = msgspec.msgpack.encode(
            MailboxAvailableResponsePacket(count=queue_len)
        )

        await self.serial_flow.send(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            response,
        )
        return True

    async def handle_read(self, seq_id: int, _: bytes) -> bool:
        if not self.state.mailbox_queue:
            message_payload = b""
            original_payload = None
        else:
            try:
                original_payload = self.state.mailbox_queue.popleft()
                message_payload = original_payload
            except IndexError:
                message_payload = b""
                original_payload = None

        max_allowed = protocol.MAX_PAYLOAD_SIZE - 3
        if len(message_payload) > max_allowed:
            logger.warning(
                "Mailbox message too long (%d bytes), truncating.", len(message_payload)
            )
            message_payload = message_payload[:max_allowed]

        # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
        response_payload = msgspec.msgpack.encode(
            MailboxReadResponsePacket(content=message_payload)
        )

        send_ok = await self.serial_flow.send(
            Command.CMD_MAILBOX_READ_RESP.value,
            response_payload,
        )

        if not send_ok:
            if original_payload is not None:
                self.state.mailbox_queue.appendleft(original_payload)
            return False

        await self._publish_available(
            "outgoing_available", len(self.state.mailbox_queue)
        )
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
        queue = self.state.mailbox_queue
        limit = self.state.mailbox_queue_limit

        if limit > 0 and len(queue) >= limit:
            # [SIL-2] Manual FIFO eviction to maintain deterministic queue size
            queue.popleft()
            self.state.mailbox_dropped_messages += 1
            self.state.mailbox_outgoing_overflow_events += 1

        queue.append(payload)

        queue_len = len(queue)
        logger.info(
            "Added message to mailbox queue. Size=%d",
            queue_len,
            queue_len=queue_len,
            queue_limit=limit,
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
            try:
                message_payload = self.state.mailbox_incoming_queue.popleft()
            except IndexError:
                await self._publish_available("incoming_available", 0)
                return
            try:
                await self.mqtt_flow.enqueue_mqtt(
                    QueuedPublish(
                        topic_name=topic,
                        payload=message_payload,
                    ),
                    reply_context=inbound,
                )
            finally:
                await self._publish_available(
                    "incoming_available", len(self.state.mailbox_incoming_queue)
                )
            return

        if not self.state.mailbox_queue:
            return

        try:
            message_payload = self.state.mailbox_queue.popleft()
        except IndexError:
            return

        try:
            await self.mqtt_flow.enqueue_mqtt(
                QueuedPublish(
                    topic_name=topic,
                    payload=message_payload,
                ),
                reply_context=inbound,
            )
        finally:
            await self._publish_available(
                "outgoing_available", len(self.state.mailbox_queue)
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
        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic_name,
                payload=str(count).encode("utf-8"),
            )
        )


__all__ = ["MailboxComponent"]
