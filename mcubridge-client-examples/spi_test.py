"""Test script for SPI service using SpiDevice with direct LocalBridgeStub and bridge_session."""

from __future__ import annotations

import asyncio
import logging
import typer
from typing import Annotated

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
app = typer.Typer(help="Test SPI service using direct LocalBridgeStub.")


async def run_test(socket_path: str | None = None, topic_prefix: str = "br") -> None:
    logging.info("--- Starting SPI Service Test ---")
    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        topic_begin = Topic.build(Topic.SPI, "begin", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_begin, payload=b"", qos=1))

        topic_transfer = Topic.build(Topic.SPI, "transfer", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_transfer, payload=b"\x01\x02\x03\x04", qos=1))
        await asyncio.sleep(1)

        topic_end = Topic.build(Topic.SPI, "end", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_end, payload=b"", qos=1))

    logging.info("--- SPI Service Test Complete ---")


@app.command()
def main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    app()
