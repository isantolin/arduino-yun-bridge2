#!/usr/bin/env python3
"""Exercise datastore interactions using direct GatewayClient and Channel."""

from __future__ import annotations

import asyncio

import structlog
import typer
from mcubridge_client import mcu_wire_pb2
from mcubridge_client.cli import DeviceIdOption, HostOption, PortOption, bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id) as (_channel, stub):
        logger.info("--- Starting DataStore Bridge Client Test ---")

        # --- Test 1: Put a new key-value pair ---
        logger.info("[Test 1: Put a new key-value pair]")
        key1: str = "client_test/temperature"
        value1: str = "25.5"

        await stub.DatastorePut(mcu_wire_pb2.DatastorePut(key=key1, value=value1.encode("utf-8")))
        logger.info("Put value to key", key=key1, value=value1)

        # --- Test 2: Get key-value pair ---
        logger.info("[Test 2: Get key-value pair]")
        resp = await stub.DatastoreGet(mcu_wire_pb2.DatastoreGet(key=key1))
        logger.info("Retrieved value", key=key1, value=resp.value.decode("utf-8"))

    logger.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:
    asyncio.run(run_test(host, port, device_id))


cli = typer.Typer(
    help="Exercise datastore interactions using direct GatewayClient through Gateway.", add_completion=False
)


@cli.command()
def cli_main(
    host: HostOption = None,
    port: PortOption = None,
    device_id: DeviceIdOption = None,
) -> None:
    main(host, port, device_id)


if __name__ == "__main__":
    cli()
