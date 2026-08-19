"""Shared CLI helpers for mcubridge client example scripts."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncGenerator

import structlog
from grpclib.client import Channel
from .definitions import build_bridge_args
from .env import dump_client_env
from .mcubridge_grpc import LocalBridgeStub


from mcubridge.config.logging import configure_logging as _central_configure_logging


def configure_logging(debug: bool | None = None, console: bool = True) -> None:
    """Set up structured logging for client examples using centralized config."""
    _central_configure_logging(debug=debug, console=console)


@contextlib.asynccontextmanager
async def bridge_session(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> AsyncGenerator[tuple[Channel, LocalBridgeStub]]:
    """Connect Channel + LocalBridgeStub and guarantee close on exit."""
    dump_client_env(structlog.get_logger(__name__))
    bridge_args = build_bridge_args(socket_path, topic_prefix)
    sock = str(
        socket_path
        or bridge_args.get("socket_path")
        or os.environ.get("MCUBRIDGE_SOCKET_PATH")
        or "/var/run/mcubridge.sock"
    )
    channel = Channel(path=sock)
    stub = LocalBridgeStub(channel)
    try:
        yield channel, stub
    finally:
        channel.close()
