#!/usr/bin/env python3
"""Example: Test process execution using the async YunBridge client.

Sends a command to the br/sh/run topic and waits for a response.
"""
import asyncio
import logging

import aiomqtt

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def handle_messages(client: aiomqtt.Client):
    """Listen for incoming messages and log them."""
    async for message in client.messages:
        logging.info("[MQTT] Response on %s:\n%s", message.topic, message.payload.decode())


async def main() -> None:
    """Run main test logic."""
    topic_cmd = "br/sh/run"
    topic_cmd_response = "br/sh/response"
    command_to_run = "echo hello_from_yun && sleep 1 && date"

    try:
        async with get_mqtt_client() as client:
            await client.subscribe(topic_cmd_response)
            logging.info("Subscribed to %s", topic_cmd_response)

            listener = asyncio.create_task(handle_messages(client))

            logging.info("Sending command to '%s': '%s'", topic_cmd, command_to_run)
            await client.publish(topic_cmd, command_to_run)

            logging.info("Waiting 3s for responses...")
            await asyncio.sleep(3)

            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    except Exception as e:
        logging.error("An error occurred: %s", e)

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
