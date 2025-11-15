#!/usr/bin/env python3
"""Example: Test generic pin control using the async YunBridge client."""
import asyncio
import logging
import sys

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def main() -> None:
    """Run main test logic."""
    dump_client_env(logging.getLogger(__name__))
    bridge = Bridge()
    await bridge.connect()

    pin = 13
    if len(sys.argv) > 1:
        try:
            pin = int(sys.argv[1])
        except ValueError:
            logging.error(f"Invalid pin number: {sys.argv[1]}")
            return

    try:
        logging.info(f"Turning pin {pin} ON")
        await bridge.digital_write(pin, 1)
        await asyncio.sleep(2)

        logging.info(f"Turning pin {pin} OFF")
        await bridge.digital_write(pin, 0)
        await asyncio.sleep(2)

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
