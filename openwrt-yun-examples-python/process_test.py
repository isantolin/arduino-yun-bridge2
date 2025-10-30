#!/usr/bin/env python3
"""Example: Test process execution using the async YunBridge client."""
import asyncio
import logging

from yunbridge_client import Bridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def main() -> None:
    """Run main test logic."""
    bridge = Bridge()
    await bridge.connect()

    command_to_run = ["echo", "hello_from_yun"]

    try:
        logging.info(f"Running command: '{' '.join(command_to_run)}'")
        # Using run_sketch_command as it's the closest equivalent in the all_features_test
        output = await bridge.run_sketch_command(command_to_run)
        logging.info(f"Command output:\n{output.decode()}")

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
