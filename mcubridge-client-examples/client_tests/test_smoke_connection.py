#!/usr/bin/env python3
"""Minimal connectivity smoke test for LocalBridgeStub and Channel using bridge_session."""

from __future__ import annotations

import asyncio
import logging
import typer
from typing import Annotated

from mcubridge_client import dump_client_env
from mcubridge_client.cli import bridge_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    dump_client_env(logging.getLogger(__name__))

    async with bridge_session(socket_path, topic_prefix) as (_channel, _stub):
        logging.info("Bridge channel initialized via bridge_session")


cli = typer.Typer(
    help="Minimal connectivity smoke test for LocalBridgeStub and Channel.",
    add_completion=False,
)


@cli.command()
def main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    cli()
