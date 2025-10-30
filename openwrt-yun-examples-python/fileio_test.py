#!/usr/bin/env python3
"""Example: Test file I/O using the async YunBridge client."""
import asyncio
import logging

from yunbridge_client import Bridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def main() -> None:
    """Run main test logic."""
    bridge = Bridge()
    await bridge.connect()

    test_filename = "/tmp/test_file.txt"
    test_content = "hello from async fileio_test"

    try:
        # --- Test File Write ---
        logging.info(f"Writing '{test_content}' to {test_filename}")
        await bridge.file_write(test_filename, test_content)

        # --- Test File Read ---
        logging.info(f"Reading from {test_filename}")
        content = await bridge.file_read(test_filename)
        logging.info(f"Read content: {content.decode()}")

        if content.decode() == test_content:
            logging.info("SUCCESS: Read content matches written content.")
        else:
            logging.error("FAILURE: Read content does not match written content.")

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        # --- Test File Remove ---
        logging.info(f"Removing {test_filename}")
        await bridge.file_remove(test_filename)
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())