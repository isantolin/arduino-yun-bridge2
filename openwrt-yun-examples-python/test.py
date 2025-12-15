#!/usr/bin/env python3
"""Minimal connectivity smoke test for the bridge client."""

import asyncio
import logging

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def main() -> None:
    dump_client_env(logging.getLogger(__name__))
    bridge = Bridge()
    await bridge.connect()
    logging.info("Bridge connected")
    await bridge.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
