#!/usr/bin/env python3
"""Bootloader trigger test script using direct LocalBridgeStub and bridge_session."""

from __future__ import annotations

import asyncio
import logging
import typer
from typing import Annotated

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bootloader_sim")


async def run_test(socket_path: str | None = None, topic_prefix: str = "br") -> None:
    log.info("Waiting 5s for link readiness...")
    await asyncio.sleep(5)

    log.info("Triggering bootloader via LocalBridgeStub...")
    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        topic_bl = Topic.build(Topic.SYSTEM, "bootloader", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_bl, payload=b"", qos=1))
        log.info("Bootloader command sent.")

    log.info("Watching for MCU output (2s)...")
    await asyncio.sleep(2)


def main(socket_path: str | None = None, topic_prefix: str = "br") -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


cli = typer.Typer(help="Bootloader trigger test script using direct LocalBridgeStub.", add_completion=False)


@cli.command()
def cli_main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(socket_path, topic_prefix)


if __name__ == "__main__":
    cli()
