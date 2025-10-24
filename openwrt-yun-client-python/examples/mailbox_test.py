#!/usr/bin/env python3
"""Example: Test mailbox feature using the async YunBridge client.

Publishes a message to br/mailbox/write and listens for responses on br/mailbox/processed.
"""
import asyncio
import logging

import aiomqtt

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def handle_messages(client: aiomqtt.Client):
    """Listen for incoming messages and log them."""
    async for message in client.messages:
        logging.info("[MQTT] Received on %s: %s", message.topic, message.payload.decode())


async def main() -> None:
    """Run main test logic."""
    topic_send = "br/mailbox/write"
    topic_recv = "br/mailbox/processed"
    message_to_send = "hello_from_async_client"

    try:
        async with get_mqtt_client() as client:
            await client.subscribe(topic_recv)
            logging.info("Subscribed to %s to listen for responses.", topic_recv)

            listener = asyncio.create_task(handle_messages(client))

            logging.info("Sending message to %s: '%s'", topic_send, message_to_send)
            await client.publish(topic_send, message_to_send)

            logging.info("Waiting for responses...")
            await asyncio.sleep(3)

            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    except Exception as e:
        logging.error("An error occurred: %s", e)

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
