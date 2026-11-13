"""Shared CLI helpers for mcubridge client example scripts. [SIL-2]"""

from __future__ import annotations

import contextlib
import ssl
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog
from mcubridge.config.logging import configure_logging as configure_logging

from .client import GatewayClient, GatewayConnection
from .definitions import build_bridge_args
from .env import dump_client_env

__all__ = ["bridge_session", "configure_logging"]


@contextlib.asynccontextmanager
async def bridge_session(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
    connection_factory: Callable[[str, int], Any] | None = None,
) -> AsyncGenerator[tuple[Any, GatewayClient]]:
    """Connect GatewayConnection + GatewayClient directly to Cloud Gateway and guarantee close on exit."""
    dump_client_env(structlog.get_logger(__name__))
    bridge_args = build_bridge_args(host=host, port=port, device_id=device_id)
    effective_host = str(bridge_args["host"])
    effective_port = int(str(bridge_args["port"]))
    effective_device = str(bridge_args["device_id"])

    connection = (
        connection_factory(effective_host, effective_port)
        if connection_factory is not None
        else GatewayConnection(host=effective_host, port=effective_port, ssl_context=ssl_context)
    )
    client = GatewayClient(connection=connection, target_device_id=effective_device)
    try:
        yield connection, client
    finally:
        connection.close()
