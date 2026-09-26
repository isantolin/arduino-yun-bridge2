#!/usr/bin/env python3
"""Minimal connectivity smoke test for LocalBridgeStub and Channel using bridge_session."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from mcubridge_client import dump_client_env
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    dump_client_env(logger)

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (
        _channel,
        _stub,
    ):
        logger.info("Bridge channel initialized via bridge_session")


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
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix))


if __name__ == "__main__":
    cli()
