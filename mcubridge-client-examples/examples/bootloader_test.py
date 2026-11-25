#!/usr/bin/env python3
"""Bootloader trigger test script using direct GatewayClient through Gateway."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from mcubridge_client import mcu_wire_pb2
from mcubridge_client.cli import DEVICE_ID_OPTION, HOST_OPTION, PORT_OPTION, bridge_session, configure_logging
from mcubridge_client.protocol import BOOTLOADER_MAGIC

configure_logging()
log = structlog.get_logger("bootloader_sim")


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:
    log.info("Waiting 5s for link readiness...")
    await asyncio.sleep(5)

    log.info("Triggering bootloader via GatewayClient...")
    async with bridge_session(host=host, port=port, device_id=device_id) as (_channel, stub):
        await stub.EnterBootloader(mcu_wire_pb2.EnterBootloader(magic=BOOTLOADER_MAGIC))
        log.info("Bootloader command sent.")

    log.info("Watching for MCU output (2s)...")
    await asyncio.sleep(2)


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:
    asyncio.run(run_test(host, port, device_id))


cli = typer.Typer(
    help="Bootloader trigger test script using direct GatewayClient through Gateway.", add_completion=False
)


@cli.command()
def cli_main(
    host: Annotated[str | None, HOST_OPTION] = None,
    port: Annotated[int | None, PORT_OPTION] = None,
    device_id: Annotated[str | None, DEVICE_ID_OPTION] = None,
) -> None:
    main(host, port, device_id)


if __name__ == "__main__":
    cli()
