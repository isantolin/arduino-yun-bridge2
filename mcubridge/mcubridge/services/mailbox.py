"""Mailbox component encapsulating MCU/Linux mailbox flows."""

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
        enqueue_mqtt: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.enqueue_mqtt = enqueue_mqtt

    async def handle_processed(self, seq_id: int, payload: bytes) -> bool:
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "processed",
        )
        try:
            packet = msgspec.msgpack.decode(payload, type=MailboxProcessedPacket)
            message_id = packet.message_id
            # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
            body = msgspec.msgpack.encode({"message_id": message_id})
        except (ValueError, msgspec.MsgspecError):
            body = payload

        await self.enqueue_mqtt(QueuedPublish(topic_name=topic_name, payload=body))
        return True

    async def handle_push(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_MAILBOX_PUSH from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=MailboxPushPacket)
            data = packet.data
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed MailboxPushPacket: %s", e)
            return False

        if len(self.state.mailbox_incoming_queue) >= self.state.mailbox_queue_limit:
            await self._report_overflow("incoming_overflow", data)
            return True

        self.state.mailbox_incoming_queue.append(data)

        topic = self.state.mailbox_incoming_topic or topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "incoming",
        )
        await self.enqueue_mqtt(QueuedPublish(topic_name=topic, payload=data))

        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic_path(
                    self.state.mqtt_topic_prefix, Topic.MAILBOX, "incoming_available"
                ),
                payload=str(len(self.state.mailbox_incoming_queue)).encode("utf-8"),
            )
        )
        return True

    async def handle_available(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_MAILBOX_AVAILABLE from MCU."""
        if payload:
            await self.serial_flow.send(Status.MALFORMED.value, b"")
            return True

        count = len(self.state.mailbox_queue)
        resp = MailboxAvailableResponsePacket(count=count)
        return await self.serial_flow.send(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value, msgspec.msgpack.encode(resp)
        )

    async def handle_read(self, seq_id: int, _: bytes) -> bool:
        """Handle CMD_MAILBOX_READ from MCU."""
        try:
            data = self.state.mailbox_queue.popleft()
            resp = MailboxReadResponsePacket(content=data)
            await self.serial_flow.send(
                Command.CMD_MAILBOX_READ_RESP.value, msgspec.msgpack.encode(resp)
            )
            await self._publish_available("available", len(self.state.mailbox_queue))
            return True
        except IndexError:
            await self.serial_flow.send(Status.ERROR.value, b"")
            return True

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Process inbound MQTT requests for mailbox operations."""
        from ..protocol.protocol import MailboxAction

        action = route.identifier
        payload = msgspec.convert(inbound.payload, bytes)

        match action:
            case MailboxAction.WRITE:
                if len(self.state.mailbox_queue) >= self.state.mailbox_queue_limit:
                    await self._report_overflow("overflow", payload, inbound)
                    return True
                self.state.mailbox_queue.append(payload)
                await self._publish_available(
                    "available", len(self.state.mailbox_queue)
                )
                return True

            case MailboxAction.READ:
                await self._handle_mqtt_read(inbound)
                return True

            case "incoming_available":
                await self._publish_available(
                    "incoming_available", len(self.state.mailbox_incoming_queue)
                )
                return True
            case _:
                return False

    async def _handle_mqtt_read(self, inbound: Message) -> None:
        topic = topic_path(
            self.state.mqtt_topic_prefix, Topic.MAILBOX, "incoming", "resp"
        )
        async with self.state.process_lock:
            try:
                message_payload = self.state.mailbox_incoming_queue.popleft()
            except IndexError:
                await self._publish_available("incoming_available", 0)
                return
            try:
                await self.enqueue_mqtt(
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

    async def _report_overflow(
        self, suffix: str, body: bytes, inbound: Message | None = None
    ) -> None:
        overflow_topic = topic_path(self.state.mqtt_topic_prefix, Topic.MAILBOX, suffix)
        properties = (("bridge-error", Topic.MAILBOX.value),) if inbound else ()

        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=overflow_topic,
                payload=body,
                content_type="application/msgpack",
                user_properties=properties,
            ),
            reply_context=inbound,
        )

    async def _publish_available(self, suffix: str, count: int) -> None:
        topic_name = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.MAILBOX,
            suffix,
        )
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic_name,
                payload=str(count).encode("utf-8"),
            )
        )


__all__ = ["MailboxComponent"]
