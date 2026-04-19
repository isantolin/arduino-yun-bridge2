"""Shared CLI helpers for mcubridge client example scripts."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

from aiomqtt import Client
from . import get_client, build_bridge_args, dump_client_env


def configure_logging() -> None:
    """Set up console logging in the standard format used by all examples."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


@contextlib.asynccontextmanager
async def bridge_session(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool = False,
) -> AsyncIterator[Client]:
    """Connect an aiomqtt.Client and guarantee disconnect on exit."""
    dump_client_env(logging.getLogger(__name__))
    bridge_args = build_bridge_args(host, port, user, password, tls_insecure)
    async with get_client(**cast("dict[str, Any]", bridge_args)) as client:
        yield client
