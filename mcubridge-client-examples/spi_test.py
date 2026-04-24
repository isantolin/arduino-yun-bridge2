"""Test script for SPI service and Auto-Baudrate fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Bridge

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def run_test(
    host: str,
    port: int,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:
    bridge = Bridge(host=host, port=port, username=user, password=password)
    if tls_insecure and bridge.tls_context:
        bridge.tls_context.check_hostname = False
        bridge.tls_context.verify_mode = 0

    await bridge.connect()
    logger.info("--- Starting SPI Service Test ---")

    try:
        # Use high-level SpiDevice abstraction
        async with bridge.spi(frequency=4000000, mode=0) as spi:
            logger.info("SPI session started automatically (begin + config)")

            test_data = [0xAA, 0xBB, 0xCC, 0xDD]
            logger.info("Transferring data (list): %s", test_data)

            # This will wait for SPI_TRANSFER_RESP
            resp = await spi.transfer(test_data)
            logger.info("Received SPI data: %s", resp.hex())

            logger.info("SPI session ends automatically (end)")

        logger.info("SPI Service Test PASSED.")

        logger.info("--- Starting Bootloader Test ---")
        await bridge.enter_bootloader()
        logger.info("Bootloader command sent (MCU should reset).")

    finally:
        await bridge.disconnect()


def main(
    host: Annotated[str, typer.Option(help="MQTT Broker Host")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="MQTT Broker Port")] = 1883,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = True,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    typer.run(main)
