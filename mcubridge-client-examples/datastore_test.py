#!/usr/bin/env python3
"""Exercise datastore interactions using direct LocalBridgeStub and Channel."""

from __future__ import annotations

import asyncio
import logging
import typer
from typing import Annotated

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
app = typer.Typer(help="Exercise datastore interactions using direct LocalBridgeStub.")


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    logging.info("--- Starting Datastore Test ---")
    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        topic_put = Topic.build(Topic.DATASTORE, "put", prefix=topic_prefix)
        payload = b"key1\x00value1"
        logging.info("Writing datastore key1=value1")
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_put, payload=payload, qos=1))
        await asyncio.sleep(2)

    logging.info("--- Datastore Test Complete ---")


@app.command()
def main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    app()
