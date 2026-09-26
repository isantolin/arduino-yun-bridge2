"""Test script for SPI service using SpiDevice with direct LocalBridgeStub and bridge_session."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from mcubridge_client import SpiDevice
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    logger.info("--- Starting SPI Service Test ---")

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        device = SpiDevice(stub=stub, frequency=4000000, topic_prefix=topic_prefix)
        async with device as spi:
            logger.info("SPI session started automatically (begin + config)")

            test_data = [0xAA, 0xBB, 0xCC, 0xDD]
            logger.info("Transferring data", data=test_data)

            resp = await spi.transfer(test_data)
            logger.info("Received SPI data", data=resp.hex())
            if resp != bytes(test_data):
                raise AssertionError(
                    f"SPI transfer response mismatch: expected {bytes(test_data).hex()}, got {resp.hex()}"
                )

            logger.info("SPI session ends automatically (end)")

        logger.info("SPI Service Test PASSED.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix))


cli = typer.Typer(help="Test SPI service using direct LocalBridgeStub through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()
