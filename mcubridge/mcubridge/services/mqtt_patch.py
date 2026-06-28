# pyright: reportPrivateUsage=false, reportCallIssue=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Monkey patch aiomqtt v3.0.0a1 to restore topic_alias support in publish(). [SIL-2]"""

from __future__ import annotations
import asyncio
from typing import Any
import aiomqtt
from aiomqtt import PublishPacket


def apply_aiomqtt_patches() -> None:
    async def patched_publish(
        self: aiomqtt.Client,
        topic: str,
        payload: bytes,
        *,
        qos: aiomqtt.QoS = aiomqtt.QoS.AT_MOST_ONCE,
        packet_id: int | None = None,
        duplicate: bool = False,
        retain: bool = False,
        message_expiry_interval: int | None = None,
        content_type: str | None = None,
        response_topic: str | None = None,
        correlation_data: bytes | None = None,
        user_properties: list[tuple[str, str]] | None = None,
        topic_alias: int | None = None,
    ) -> Any:
        match qos:
            case aiomqtt.QoS.AT_MOST_ONCE:
                await self._publish_at_most_once(
                    topic,
                    payload,
                    retain=retain,
                    message_expiry_interval=message_expiry_interval,
                    content_type=content_type,
                    response_topic=response_topic,
                    correlation_data=correlation_data,
                    user_properties=user_properties,
                    topic_alias=topic_alias,
                )
                return None
            case aiomqtt.QoS.AT_LEAST_ONCE:
                if packet_id is None:
                    msg = "Packet ID required for QoS=1 PUBLISH"
                    raise ValueError(msg)
                return await self._publish_at_least_once(
                    topic,
                    packet_id,
                    payload,
                    duplicate=duplicate,
                    retain=retain,
                    message_expiry_interval=message_expiry_interval,
                    content_type=content_type,
                    response_topic=response_topic,
                    correlation_data=correlation_data,
                    user_properties=user_properties,
                    topic_alias=topic_alias,
                )
            case aiomqtt.QoS.EXACTLY_ONCE:
                if packet_id is None:
                    msg = "Packet ID required for QoS=2 PUBLISH"
                    raise ValueError(msg)
                return await self._publish_exactly_once(
                    topic,
                    packet_id,
                    payload,
                    duplicate=duplicate,
                    retain=retain,
                    message_expiry_interval=message_expiry_interval,
                    content_type=content_type,
                    response_topic=response_topic,
                    correlation_data=correlation_data,
                    user_properties=user_properties,
                    topic_alias=topic_alias,
                )

    async def patched_publish_at_most_once(
        self: aiomqtt.Client,
        topic: str,
        payload: bytes,
        *,
        retain: bool = False,
        message_expiry_interval: int | None = None,
        content_type: str | None = None,
        response_topic: str | None = None,
        correlation_data: bytes | None = None,
        user_properties: list[tuple[str, str]] | None = None,
        topic_alias: int | None = None,
    ) -> None:
        await self._send(
            PublishPacket(
                topic=topic,
                payload=payload,
                qos=aiomqtt.QoS.AT_MOST_ONCE,
                retain=retain,
                message_expiry_interval=message_expiry_interval,
                content_type=content_type,
                response_topic=response_topic,
                correlation_data=correlation_data,
                user_properties=user_properties,
                topic_alias=topic_alias,
            )
        )

    async def patched_publish_at_least_once(
        self: aiomqtt.Client,
        topic: str,
        packet_id: int,
        payload: bytes,
        *,
        duplicate: bool = False,
        retain: bool = False,
        message_expiry_interval: int | None = None,
        content_type: str | None = None,
        response_topic: str | None = None,
        correlation_data: bytes | None = None,
        user_properties: list[tuple[str, str]] | None = None,
        topic_alias: int | None = None,
    ) -> Any:
        if not hasattr(self, "_send_semaphore"):
            raise aiomqtt.ConnectError(self._endpoint)
        await self._send_semaphore.acquire()
        self._pending_pubacks[packet_id] = asyncio.Future()
        try:
            await self._send(
                PublishPacket(
                    packet_id=packet_id,
                    topic=topic,
                    payload=payload,
                    qos=aiomqtt.QoS.AT_LEAST_ONCE,
                    duplicate=duplicate,
                    retain=retain,
                    message_expiry_interval=message_expiry_interval,
                    content_type=content_type,
                    response_topic=response_topic,
                    correlation_data=correlation_data,
                    user_properties=user_properties,
                    topic_alias=topic_alias,
                )
            )
            puback_packet = await self._disconnected_or(self._pending_pubacks[packet_id])
        finally:
            del self._pending_pubacks[packet_id]
        from mqtt5 import PubAckReasonCode

        if puback_packet.reason_code not in (
            PubAckReasonCode.SUCCESS,
            PubAckReasonCode.NO_MATCHING_SUBSCRIBERS,
        ):
            raise aiomqtt.NegativeAckError(puback_packet)
        return puback_packet

    async def patched_publish_exactly_once(
        self: aiomqtt.Client,
        topic: str,
        packet_id: int,
        payload: bytes,
        *,
        duplicate: bool = False,
        retain: bool = False,
        message_expiry_interval: int | None = None,
        content_type: str | None = None,
        response_topic: str | None = None,
        correlation_data: bytes | None = None,
        user_properties: list[tuple[str, str]] | None = None,
        topic_alias: int | None = None,
    ) -> Any:
        if not hasattr(self, "_send_semaphore"):
            raise aiomqtt.ConnectError(self._endpoint)
        await self._send_semaphore.acquire()
        self._pending_pubrecs[packet_id] = asyncio.Future()
        try:
            await self._send(
                PublishPacket(
                    topic=topic,
                    payload=payload,
                    qos=aiomqtt.QoS.EXACTLY_ONCE,
                    duplicate=duplicate,
                    retain=retain,
                    packet_id=packet_id,
                    message_expiry_interval=message_expiry_interval,
                    content_type=content_type,
                    response_topic=response_topic,
                    correlation_data=correlation_data,
                    user_properties=user_properties,
                    topic_alias=topic_alias,
                )
            )
            pubrec_packet = await self._disconnected_or(self._pending_pubrecs[packet_id])
        finally:
            del self._pending_pubrecs[packet_id]
        from mqtt5 import PubRecReasonCode

        if pubrec_packet.reason_code != PubRecReasonCode.SUCCESS:
            raise aiomqtt.NegativeAckError(pubrec_packet)
        return pubrec_packet

    aiomqtt.Client.publish = patched_publish
    aiomqtt.Client._publish_at_most_once = patched_publish_at_most_once
    aiomqtt.Client._publish_at_least_once = patched_publish_at_least_once
    aiomqtt.Client._publish_exactly_once = patched_publish_exactly_once
