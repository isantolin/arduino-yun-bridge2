#!/usr/bin/env python3
"""Example that listens for mailbox messages pushed from the Yun daemon."""

import asyncio
import logging
import argparse

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Mailbox read test.")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    args = parser.parse_args()

    dump_client_env(logger)

    bridge_args = {}
    if args.host:
        bridge_args["host"] = args.host
    if args.port:
        bridge_args["port"] = args.port
    if args.user:
        bridge_args["username"] = args.user
    if args.password:
        bridge_args["password"] = args.password

    bridge = Bridge(**bridge_args)
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
