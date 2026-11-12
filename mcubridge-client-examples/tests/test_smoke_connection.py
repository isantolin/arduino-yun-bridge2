#!/usr/bin/env python3
"""Minimal connectivity smoke test for LocalBridgeStub and Channel using bridge_session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

import structlog
import typer
from mcubridge_client import dump_client_env
from mcubridge_client.cli import bridge_session, configure_logging

logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    session_factory: Any = bridge_session,
) -> None:
    dump_client_env(logger)

    async with session_factory(host=host, port=port, device_id=device_id) as (_channel, _stub):
        logger.info("Bridge channel initialized via bridge_session")


executor_fn: Callable[..., Any] = run_test

cli = typer.Typer(
    help="Minimal connectivity smoke test for LocalBridgeStub and Channel through Gateway.",
    add_completion=False,
)


@cli.command()
def main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
) -> None:
    configure_logging()
    asyncio.run(executor_fn(host, port, device_id))


if __name__ == "__main__":
    cli()
