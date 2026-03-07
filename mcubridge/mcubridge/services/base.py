"""Base interfaces for service components."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Deque, Protocol, TypeVar

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..protocol.structures import QueuedPublish
from ..state.context import RuntimeState

TReq = TypeVar("TReq")

logger = logging.getLogger("mcubridge.services")


class BridgeContext(Protocol):
    """Protocol describing the surface required by service components."""

    config: RuntimeConfig
    state: RuntimeState

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool: ...

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None: ...

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
    ) -> None: ...

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]: ...


class BaseComponent:
    """Base class for services providing shared boilerplate reduction."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState, ctx: BridgeContext) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx

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
        except Exception:
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


__all__ = ["BridgeContext", "BaseComponent"]
