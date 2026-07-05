"""Test script for SPI service and Auto-Baudrate fallback."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client import Bridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    bridge = Bridge(socket_path=socket_path, topic_prefix=topic_prefix)

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
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test script for SPI service and Auto-Baudrate fallback.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)
