"""Test script for SPI service and Auto-Baudrate fallback."""

from __future__ import annotations

import argparse
import asyncio
import logging

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
    host: str = "127.0.0.1",
    port: int = 1883,
    user: str | None = None,
    password: str | None = None,
    tls_insecure: bool = True,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test script for SPI service and Auto-Baudrate fallback."
    )
    parser.add_argument("--host", default="127.0.0.1", help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        default=True,
        help="Disable TLS certificate verification",
    )
    _args = parser.parse_args()
    main(_args.host, _args.port, _args.user, _args.password, _args.tls_insecure)
