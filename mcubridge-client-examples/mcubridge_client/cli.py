"""Shared CLI helpers for mcubridge client example scripts."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncGenerator

from grpclib.client import Channel
from .definitions import build_bridge_args
from .env import dump_client_env
from .mcubridge_grpc import LocalBridgeStub


def configure_logging() -> None:
    """Set up console logging in the standard format used by all examples."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


@contextlib.asynccontextmanager
async def bridge_session(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> AsyncGenerator[tuple[Channel, LocalBridgeStub]]:
    """Connect Channel + LocalBridgeStub and guarantee close on exit."""
    dump_client_env(logging.getLogger(__name__))
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
