#!/usr/bin/env python3
"""Example: Test process execution using the async YunBridge client."""
import asyncio
import logging

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

    command_to_run: list[str] = ["echo", "hello_from_yun"]

    try:
        logging.info(f"Running command: '{' '.join(command_to_run)}'")
        # Using run_sketch_command as it's the closest equivalent to the
        # helper showcased in all_features_test.
        output = await bridge.run_sketch_command(command_to_run)
        logging.info("Command output:\n%s", output.decode())

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
