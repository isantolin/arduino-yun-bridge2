#!/usr/bin/env python3
"""Bootloader trigger test script using direct LocalBridgeStub through Gateway."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
log = structlog.get_logger("bootloader_sim")


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    log.info("Waiting 5s for link readiness...")
    await asyncio.sleep(5)

    log.info("Triggering bootloader via LocalBridgeStub...")
    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        topic_bl = Topic.build(Topic.SYSTEM, "bootloader", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_bl, payload=b"", qos=1))
        log.info("Bootloader command sent.")

    log.info("Watching for MCU output (2s)...")
    await asyncio.sleep(2)


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix))


cli = typer.Typer(
    help="Bootloader trigger test script using direct LocalBridgeStub through Gateway.", add_completion=False
)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[str | None, typer.Option("--device-id", help="Target device ID")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()
