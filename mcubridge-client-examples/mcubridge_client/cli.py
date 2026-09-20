"""Shared CLI helpers for mcubridge client example scripts."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import grpclib.events
from grpclib.client import Channel
import structlog

from mcubridge.config.logging import configure_logging as _central_configure_logging

from .definitions import DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT, build_bridge_args
from .env import dump_client_env
from .mcubridge_grpc import LocalBridgeStub


def configure_logging(debug: bool | None = None, console: bool = True) -> None:
    """Set up structured logging for client examples using centralized config."""
    _central_configure_logging(debug=debug, console=console)


@contextlib.asynccontextmanager
async def bridge_session(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> AsyncGenerator[tuple[Channel, LocalBridgeStub]]:
    """Connect Channel + LocalBridgeStub directly to Cloud Gateway and guarantee close on exit."""
    dump_client_env(structlog.get_logger(__name__))
    bridge_args = build_bridge_args(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix)
    effective_host = str(bridge_args.get("host") or DEFAULT_GATEWAY_HOST)
    effective_port = int(bridge_args.get("port") or DEFAULT_GATEWAY_PORT)
    effective_device = str(bridge_args["device_id"])

    channel = Channel(host=effective_host, port=effective_port)

    async def _inject_device_metadata(event: grpclib.events.SendRequest) -> None:
        event.metadata["x-device-id"] = effective_device

    grpclib.events.listen(channel, grpclib.events.SendRequest, _inject_device_metadata)
    stub = LocalBridgeStub(channel)
    try:
        yield channel, stub
    finally:
        channel.close()
