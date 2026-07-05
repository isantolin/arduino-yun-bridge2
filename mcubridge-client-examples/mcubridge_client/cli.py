"""Shared CLI helpers for mcubridge client example scripts."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Any, cast

from . import Bridge, build_bridge_args, dump_client_env


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
) -> AsyncGenerator[Bridge]:
    """Connect a Bridge and guarantee disconnect on exit."""
    dump_client_env(logging.getLogger(__name__))
    bridge_args = build_bridge_args(socket_path, topic_prefix)
    bridge = Bridge(**cast("dict[str, Any]", bridge_args))
    await bridge.connect()
    try:
        yield bridge
    finally:
        await bridge.disconnect()
