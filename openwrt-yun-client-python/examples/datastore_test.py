"""Example: Test the DataStore functionality of the Yun Bridge using an async client.

This script tests the key-value store functionality of the Yun Bridge by
publishing and subscribing to MQTT topics.
"""
import asyncio
import logging

import aiomqtt

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# MQTT settings
TOPIC_BRIDGE = "br"


async def listen_for_value(client: aiomqtt.Client, key: str, response_queue: asyncio.Queue):
    """A task that waits for a specific datastore 'get' response."""
    topic_to_wait_for = f"{TOPIC_BRIDGE}/datastore/get/{key}"
    async for message in client.messages:
        if message.topic.value == topic_to_wait_for:
            await response_queue.put(message.payload.decode())
            return # Stop listening once the value is found 


async def main() -> None:
    """Run main test logic."""
    try:
        async with get_mqtt_client() as client:
            logging.info("--- Starting DataStore MQTT Test ---")

            response_queue = asyncio.Queue()

            # --- Test 1: Put and Get a new key-value pair ---
            logging.info("[Test 1: Put and Get a new key-value pair]")
            key1 = "mqtt_test/temperature"
            value1 = "25.5"
            topic_put_1 = f"{TOPIC_BRIDGE}/datastore/put/{key1}"
            topic_get_1 = f"{TOPIC_BRIDGE}/datastore/get/{key1}"

            await client.subscribe(topic_get_1)
            await client.publish(topic_put_1, value1)
            logging.info("Published to %s: %s", topic_put_1, value1)

            # Wait for the response from the daemon's automatic re-publish
            try:
                listener = asyncio.create_task(listen_for_value(client, key1, response_queue))
                retrieved_value = await asyncio.wait_for(response_queue.get(), timeout=5.0)
                if retrieved_value == value1:
                    logging.info("SUCCESS: Retrieved value '%s' matches put value '%s'.", retrieved_value, value1)
                else:
                    logging.error("FAILURE: Retrieved value '%s' does not match put value '%s'.", retrieved_value, value1)
            except asyncio.TimeoutError:
                logging.error("FAILURE: Timed out waiting for response for key '%s'.", key1)
            finally:
                listener.cancel()

            # --- Test 2: Get a non-existent key ---
            logging.info("\n[Test 2: Get a non-existent key]")
            key2 = "non_existent/key"
            topic_get_2 = f"{TOPIC_BRIDGE}/datastore/get/{key2}"
            await client.subscribe(topic_get_2)
            await client.publish(f"{TOPIC_BRIDGE}/datastore/get/{key2}", "") # Trigger a get request

            try:
                listener = asyncio.create_task(listen_for_value(client, key2, response_queue))
                retrieved_value_2 = await asyncio.wait_for(response_queue.get(), timeout=5.0)
                # Expecting an empty payload for a non-existent key
                if retrieved_value_2 == "":
                    logging.info("SUCCESS: Correctly received empty value for non-existent key '%s'.", key2)
                else:
                    logging.error("FAILURE: Incorrectly received value '%s' for non-existent key.", retrieved_value_2)
            except asyncio.TimeoutError:
                logging.error("FAILURE: Timed out waiting for response for non-existent key '%s'.", key2)
            finally:
                listener.cancel()

            logging.info("\n--- Test Complete ---")

    except Exception as e:
        logging.error("An error occurred: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
