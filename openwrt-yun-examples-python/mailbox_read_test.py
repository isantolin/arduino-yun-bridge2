#!/usr/bin/env python3
"""Example that listens for mailbox messages pushed from the Yun daemon."""

import asyncio
import logging

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    dump_client_env(logger)
    bridge = Bridge()
    await bridge.connect()

    try:
        logger.info("Waiting for mailbox messages. Press Ctrl+C to stop.")
        while True:
            message: bytes | None = await bridge.mailbox_read(timeout=10)
            if message is None:
                logger.info("No mailbox message within timeout; still listening...")
                continue

            preview = message.decode("utf-8", errors="ignore")
            logger.info(
                "Received mailbox message (%d bytes): %s",
                len(message),
                preview,
            )
    finally:
        await bridge.disconnect()
        logger.info("Disconnected from MQTT broker.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")
