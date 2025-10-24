#!/usr/bin/env python3
"""Example: Test all features of YunBridge v2 using the async client.

- Generic pin control (default: 13, can specify any pin)
- Key-value store
- File I/O (direct via `br/file/...` topics)
- Mailbox (topic br/mailbox/write)
- Process execution

Usage:
    python3 all_features_test.py [PIN]
"""
import asyncio
import logging
import sys

import aiomqtt

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def handle_messages(client: aiomqtt.Client):
    """Listen for incoming messages and log them."""
    async for message in client.messages:
        logging.info("[MQTT] Received on %s: %s", message.topic, message.payload.decode())


async def main() -> None:
    """Run main test logic."""
    pin = "13"
    if len(sys.argv) > 1:
        pin = sys.argv[1]

    is_analog = pin.upper().startswith("A")
    pin_function_short = "a" if is_analog else "d"
    pin_number = pin[1:] if is_analog else pin

    # --- Topic Definitions ---
    topic_set = f"br/{pin_function_short}/{pin_number}"
    topic_state = f"br/{pin_function_short}/{pin_number}/value"
    topic_cmd = "br/sh/run"
    topic_mailbox_send = "br/mailbox/write"
    topic_mailbox_recv = "br/mailbox/available"
    topic_cmd_response = "br/sh/response"
    topic_console_out = "br/console/out"

    test_filename = "all_features_test.txt"
    test_content = "hello from async all_features_test"
    topic_file_write = f"br/file/write/{test_filename}"
    topic_file_read = f"br/file/read/{test_filename}"
    topic_file_read_resp = f"br/file/read/response/{test_filename}"
    topic_file_remove = f"br/file/remove/{test_filename}"

    try:
        async with get_mqtt_client() as client:
            # Subscribe to all response topics
            await client.subscribe(topic_state)
            await client.subscribe(topic_cmd_response)
            await client.subscribe(topic_mailbox_recv)
            await client.subscribe(topic_console_out)
            await client.subscribe(topic_file_read_resp)
            await client.subscribe("br/datastore/get/foo")

            listener = asyncio.create_task(handle_messages(client))
            await asyncio.sleep(1)

            logging.info("--- Testing Pin %s ---", pin)
            logging.info("Turning pin %s ON via MQTT...", pin)
            await client.publish(topic_set, "1")
            await asyncio.sleep(1)
            logging.info("Turning pin %s OFF via MQTT...", pin)
            await client.publish(topic_set, "0")
            await asyncio.sleep(1)

            logging.info("--- Testing Key-Value Store ---")
            logging.info("Setting key 'foo' to 'bar'...")
            await client.publish("br/datastore/put/foo", "bar")
            await asyncio.sleep(1)
            logging.info("Getting key 'foo'...")
            await client.publish("br/datastore/get/foo", "")
            await asyncio.sleep(1)

            logging.info("--- Testing File I/O ---")
            logging.info("Writing to '%s'...", test_filename)
            await client.publish(topic_file_write, test_content)
            await asyncio.sleep(1)

            logging.info("Reading from '%s'...", test_filename)
            await client.publish(topic_file_read, "")
            await asyncio.sleep(1)

            logging.info("Removing file '%s'...", test_filename)
            await client.publish(topic_file_remove, "")
            await asyncio.sleep(1)

            logging.info("--- Testing Mailbox ---")
            logging.info("Sending message to mailbox...")
            await client.publish(topic_mailbox_send, "hello_from_async_mqtt")
            await asyncio.sleep(1)

            logging.info("--- Testing Process Execution ---")
            logging.info("Running 'echo hello_from_yun'...")
            await client.publish(topic_cmd, "echo hello_from_yun")
            await asyncio.sleep(1)

            logging.info("Done testing. Waiting 3s for final responses...")
            await asyncio.sleep(3)

            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    except Exception as e:
        logging.error("An error occurred: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
