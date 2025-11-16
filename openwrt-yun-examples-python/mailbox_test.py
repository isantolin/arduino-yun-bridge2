#!/usr/bin/env python3
"""Example: Test mailbox feature using the async YunBridge client."""
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

    message_to_send: str = "hello_from_async_client"

    try:
        logging.info("Sending message to mailbox: '%s'", message_to_send)
        await bridge.mailbox_write(message_to_send)
        logging.info(
            "Message sent. A listener would be needed to confirm processing."
        )

        await asyncio.sleep(3)

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
