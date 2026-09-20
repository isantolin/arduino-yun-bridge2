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
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
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
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix))


cli = typer.Typer(
    help="Exercise datastore interactions using direct LocalBridgeStub through Gateway.", add_completion=False
)


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
