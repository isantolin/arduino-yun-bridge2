#!/usr/bin/env python3
"""Example: Run an async shell command via direct LocalBridgeStub Publish call."""

from __future__ import annotations

import asyncio
import shlex
from typing import Annotated

import structlog
import typer
from mcubridge_client import pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        command_to_run = ["echo", "hello from shell"]
        cmd_str = shlex.join(command_to_run)
        logger.info("Launching command", command=cmd_str)

        res = await stub.ProcessRunAsync(pb.ProcessRunAsync(command=cmd_str))
        logger.info(
            "Shell process launched via gRPC",
            pid=res.pid,
        )

    logger.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix))


cli = typer.Typer(help="Run an async shell command via direct LocalBridgeStub through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()
