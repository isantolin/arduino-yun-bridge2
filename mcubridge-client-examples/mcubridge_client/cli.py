"""Shared CLI helpers for mcubridge client example scripts. [SIL-2]"""

from __future__ import annotations

import contextlib
import os
import ssl
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog
from mcubridge.config.logging import configure_logging as configure_logging

from .client import GatewayClient, GatewayConnection
from .definitions import build_bridge_args
from .env import dump_client_env

__all__ = ["bridge_session", "build_gateway_ssl_context", "configure_logging"]


def build_gateway_ssl_context() -> ssl.SSLContext | None:
    """Build the mTLS context for the standalone Gateway from MCUBRIDGE_GATEWAY_* settings.

    The Gateway (ADR-0003) requires TLS 1.3 and a client certificate carrying a
    ``spiffe://mcubridge/client/<id>`` URI SAN. Returns None when no CA is configured.
    """
    cafile = os.environ.get("MCUBRIDGE_GATEWAY_CAFILE")
    if not cafile:
        return None
    certfile = os.environ.get("MCUBRIDGE_GATEWAY_CERTFILE")
    keyfile = os.environ.get("MCUBRIDGE_GATEWAY_KEYFILE")
    if bool(certfile) != bool(keyfile):
        raise ValueError("MCUBRIDGE_GATEWAY_CERTFILE and MCUBRIDGE_GATEWAY_KEYFILE must be set together")
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if certfile and keyfile:
        context.load_cert_chain(certfile, keyfile)
    return context


@contextlib.asynccontextmanager
async def bridge_session(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
    connection_factory: Callable[[str, int], Any] | None = None,
    timeout_ms: int | None = None,
) -> AsyncGenerator[tuple[Any, GatewayClient]]:
    """Connect GatewayConnection + GatewayClient directly to Cloud Gateway and guarantee close on exit."""
    logger = structlog.get_logger(__name__)
    logger.info("bridge_session: resolving connection settings")
    dump_client_env(logger)
    bridge_args = build_bridge_args(host=host, port=port, device_id=device_id)
    effective_host = str(bridge_args["host"])
    effective_port = int(str(bridge_args["port"]))
    effective_device = str(bridge_args["device_id"])

    logger.info(
        "bridge_session: creating GatewayConnection",
        host=effective_host,
        port=effective_port,
        device_id=effective_device,
    )
    connection = (
        connection_factory(effective_host, effective_port)
        if connection_factory is not None
        else GatewayConnection(
            host=effective_host,
            port=effective_port,
            ssl_context=ssl_context if ssl_context is not None else build_gateway_ssl_context(),
        )
    )
    logger.info("bridge_session: GatewayConnection created")
    client = GatewayClient(connection=connection, target_device_id=effective_device, default_timeout_ms=timeout_ms)
    try:
        logger.info("bridge_session: yielding initialized client")
        yield connection, client
        logger.info("bridge_session: client context completed")
    finally:
        logger.info("bridge_session: closing GatewayConnection")
        connection.close()
        logger.info("bridge_session: GatewayConnection close returned")
