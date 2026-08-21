#!/usr/bin/env python3
"""Example: Run an async shell command via direct LocalBridgeStub Publish call."""

from __future__ import annotations

import asyncio
import shlex
import structlog
import typer
from typing import Annotated

from mcubridge_client import pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
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
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


cli = typer.Typer(help="Run an async shell command via direct LocalBridgeStub Publish.", add_completion=False)


@cli.command()
def cli_main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(socket_path, topic_prefix)


if __name__ == "__main__":
    cli()
