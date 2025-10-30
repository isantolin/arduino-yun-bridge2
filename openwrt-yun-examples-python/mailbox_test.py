#!/usr/bin/env python3
"""Example: Test mailbox feature using the async YunBridge client."""
import asyncio
import logging

from yunbridge_client import Bridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def main() -> None:
    """Run main test logic."""
    bridge = Bridge()
    await bridge.connect()

    message_to_send = "hello_from_async_client"

    try:
        logging.info(f"Sending message to mailbox: '{message_to_send}'")
        await bridge.mailbox_write(message_to_send)
        logging.info("Message sent. A listener would be needed to confirm processing.")

        await asyncio.sleep(3)

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
