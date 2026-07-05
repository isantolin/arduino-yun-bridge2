#!/usr/bin/env python3
import logging
import asyncio
from mcubridge_client import Bridge


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("bootloader_sim")

    # Handshake completes in ~20s in the emulator
    log.info("Waiting 30s for Full Link Synchronization...")
    await asyncio.sleep(30)

    log.info("Triggering bootloader via UNIX socket...")
    bridge = Bridge()
    await bridge.connect()
    try:
        await bridge.enter_bootloader()
        log.info("Bootloader command sent.")
    finally:
        await bridge.disconnect()

    log.info("Watching for MCU output (5s)...")
    await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())
