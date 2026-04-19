"""Test script for SPI service using direct MQTT."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Topic, SpiDevice
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = logging.getLogger(__name__)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:
    async with bridge_session(host, port, user, password, tls_insecure) as client:
        logger.info("--- Starting SPI Service Direct MQTT Test ---")

        try:
            # Use SpiDevice abstraction (now uses client directly)
            async with SpiDevice(client, frequency=4000000, mode=0) as spi:
                logger.info("SPI session started automatically (begin + config)")

                test_data = [0xAA, 0xBB, 0xCC, 0xDD]
                logger.info("Transferring data (list): %s", test_data)

                # This will wait for SPI_TRANSFER_RESP
                resp = await spi.transfer(test_data)
                logger.info("Received SPI data: %s", resp.hex())

                logger.info("SPI session ends automatically (end)")

            logger.info("SPI Service Test PASSED.")

            logger.info("--- Starting Bootloader Test ---")
            await client.publish(str(Topic.build(Topic.SYSTEM, "bootloader")), b"")
            logger.info("Bootloader command sent (MCU should reset).")

        except Exception as e:
            logger.error("Test failed: %s", e)
            raise


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


app = typer.Typer()

if __name__ == "__main__":
    app()
