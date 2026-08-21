#!/usr/bin/env python3
"""Exercise datastore interactions using direct LocalBridgeStub and Channel."""

from __future__ import annotations

import asyncio
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
        logger.info("--- Starting DataStore Bridge Client Test ---")

        # --- Test 1: Put a new key-value pair ---
        logger.info("[Test 1: Put a new key-value pair]")
        key1: str = "client_test/temperature"
        value1: str = "25.5"

        await stub.DatastorePut(pb.DatastorePut(key=key1, value=value1.encode("utf-8")))
        logger.info("Put value to key", key=key1, value=value1)

        # --- Test 2: Get key-value pair ---
        logger.info("[Test 2: Get key-value pair]")
        resp = await stub.DatastoreGet(pb.DatastoreGet(key=key1))
        logger.info("Retrieved value", key=key1, value=resp.value.decode("utf-8"))

    logger.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


cli = typer.Typer(help="Exercise datastore interactions using direct LocalBridgeStub.", add_completion=False)


@cli.command()
def cli_main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(socket_path, topic_prefix)


if __name__ == "__main__":
    cli()
