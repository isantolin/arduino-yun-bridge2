"""Test script for SPI service using SpiDevice with direct LocalBridgeStub and bridge_session."""

from __future__ import annotations

import asyncio
import logging
import typer
from typing_extensions import Annotated

from mcubridge_client import SpiDevice
from mcubridge_client.cli import bridge_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    logger.info("--- Starting SPI Service Test ---")

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        device = SpiDevice(stub=stub, frequency=4000000, topic_prefix=topic_prefix)
        async with device as spi:
            logger.info("SPI session started automatically (begin + config)")

            test_data = [0xAA, 0xBB, 0xCC, 0xDD]
            logger.info("Transferring data (list): %s", test_data)

            resp = await spi.transfer(test_data)
            logger.info("Received SPI data: %s", resp.hex())

            logger.info("SPI session ends automatically (end)")

        logger.info("SPI Service Test PASSED.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


cli = typer.Typer(help="Test SPI service using direct LocalBridgeStub.", add_completion=False)


@cli.command()
def cli_main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(socket_path, topic_prefix)


if __name__ == "__main__":
    cli()
