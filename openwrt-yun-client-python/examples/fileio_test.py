#!/usr/bin/env python3
"""Example: Test file I/O using the async YunBridge client.

Uses the `br/file/...` topics to directly interact with the daemon.
"""
import asyncio
import logging

import aiomqtt

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def handle_messages(client: aiomqtt.Client):
    """Listen for incoming messages and log them."""
    async for message in client.messages:
        logging.info(
            "[MQTT] File content received from %s: %s",
            message.topic,
            message.payload.decode(),
        )


async def main() -> None:
    """Run main test logic."""
    test_filename = "test_file.txt"
    test_content = "hello from async fileio_test"

    topic_write = f"br/file/write/{test_filename}"
    topic_read = f"br/file/read/{test_filename}"
    topic_read_response = f"br/file/read/response/{test_filename}"
    topic_remove = f"br/file/remove/{test_filename}"

    try:
        async with get_mqtt_client() as client:
            await client.subscribe(topic_read_response)
            logging.info("Subscribed to %s to see responses.", topic_read_response)

            listener = asyncio.create_task(handle_messages(client))

            # --- Test File Write ---
            logging.info("Writing to %s via MQTT...", test_filename)
            await client.publish(topic_write, test_content)
            await asyncio.sleep(2)

            # --- Test File Read ---
            logging.info("Reading from %s via MQTT...", test_filename)
            await client.publish(topic_read, "")  # Payload is ignored for read
            logging.info("Waiting 3s for read response...")
            await asyncio.sleep(3)

            # --- Test File Remove ---
            logging.info("Removing %s via MQTT...", test_filename)
            await client.publish(topic_remove, "")  # Payload is ignored for remove
            await asyncio.sleep(1)

            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    except Exception as e:
        logging.error("An error occurred: %s", e)

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())