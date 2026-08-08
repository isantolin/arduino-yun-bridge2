#!/usr/bin/env python3
"""Exercise datastore interactions using direct LocalBridgeStub and Channel."""

from __future__ import annotations

import asyncio
import logging
import typer
from typing_extensions import Annotated

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        logging.info("--- Starting DataStore Bridge Client Test ---")

        # --- Test 1: Put a new key-value pair ---
        logging.info("[Test 1: Put a new key-value pair]")
        key1: str = "client_test/temperature"
        value1: str = "25.5"

        topic_ds = Topic.build(Topic.DATASTORE, "put", key1, prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_ds, payload=value1.encode("utf-8"), qos=1))
        logging.info(f"Put value '{value1}' to key '{key1}'")

    logging.info("Done.")


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
