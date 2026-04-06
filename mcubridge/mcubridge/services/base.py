"""Base interfaces for service components."""

from __future__ import annotations

import asyncio
import contextlib
import structlog
from collections.abc import Callable, Coroutine
from typing import Any, Deque, Protocol, TypeVar

import msgspec
from aiomqtt.message import Message


from ..config.settings import RuntimeConfig
from ..protocol.structures import QueuedPublish, TopicRoute
from ..state.context import RuntimeState

TReq = TypeVar("TReq")

logger = structlog.get_logger("mcubridge.services")


class BridgeContext(Protocol):
    """Protocol describing the surface required by service components."""

    config: RuntimeConfig
    state: RuntimeState

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        ...

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        ...

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: Message | None = None,
    ) -> None:
        ...

    async def acknowledge_mcu_frame(
        self,
        command_id: int,
        seq_id: int,
        *,
        status: Any = None,
    ) -> None:
        ...

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        ...


class BaseComponent:
    """Base class for services providing shared boilerplate reduction."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState, ctx: BridgeContext) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle an inbound MQTT message routed to this service.

        Subclasses override this to implement topic-specific logic.
        Returns True if the message was handled, False otherwise.
        """
        return False

    @staticmethod
    def _payload_bytes(payload: Any) -> bytes:
        """Extract bytes from an MQTT message payload."""
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, bytearray):
            return bytes(payload)
        if isinstance(payload, memoryview):
            return payload.tobytes()
        try:
            return msgspec.convert(payload, bytes)
        except (msgspec.MsgspecError, TypeError, ValueError):
            return b"" if payload is None else str(payload).encode("utf-8")

    @contextlib.asynccontextmanager
    async def _track_transaction(
        self,
        queue: Deque[TReq],
        request: TReq | None,
        limit: int,
        on_overflow: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ):
        """Manages the lifecycle of a pending request transaction.

        If request is None, tracking is skipped but the overflow check still applies.
        """
        if len(queue) >= limit:
            if on_overflow:
                await on_overflow()
            yield False
            return

        if request is not None:
            queue.append(request)

        try:
            # yield to allow sending the frame
            yield True
        except (asyncio.CancelledError, Exception):
            if request is not None:
                with contextlib.suppress(ValueError):
                    queue.remove(request)
            raise

    async def _safe_send_request(
        self,
        queue: Deque[TReq],
        request: TReq | None,
        limit: int,
        command_id: int,
        payload: bytes,
        on_overflow: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> bool:
        """Helper to send a frame and track it in a queue with automatic cleanup."""
        async with self._track_transaction(queue, request, limit, on_overflow) as allowed:
            if not allowed:
                return False

            ok = await self.ctx.send_frame(command_id, payload)
            if not ok and request is not None:
                with contextlib.suppress(ValueError):
                    queue.remove(request)
            return ok

    async def _publish_value(
        self,
        topic: str,
        payload: bytes | str,
        expiry: int,
        reply_context: Message | None = None,
        content_type: str = "text/plain; charset=utf-8",
        properties: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Centralized helper for broadcasting a value and sending a targeted reply."""
        # Broadcast to all subscribers
        await self.ctx.publish(
            topic=topic,
            payload=payload,
            expiry=expiry,
            content_type=content_type,
            reply_to=None,
            properties=properties,
        )
        # Targeted reply if context exists
        if reply_context is not None:
            await self.ctx.publish(
                topic=topic,
                payload=payload,
                expiry=expiry,
                content_type=content_type,
                reply_to=reply_context,
                properties=properties,
            )

    def _decode_payload(self, packet_cls: Any, payload: bytes, command_id: Any) -> Any | None:
        """Safely decode an RPC payload using the provided packet class."""
        try:
            return packet_cls.decode(payload, command_id)
        except (ValueError):
            logger.warning("Malformed %s payload: %s", packet_cls.__name__, payload.hex())
            return None


__all__ = ["BridgeContext", "BaseComponent"]
