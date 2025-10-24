"""Example: Periodically read a sensor value from a pin using an async client.

This script demonstrates how to periodically request a reading from a digital or
analog pin and log the received value.
"""
import asyncio
import logging

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Configuration ---
# The pin to read. Use a format like 'd13' for digital or 'a0' for analog.
PIN_TO_READ = "d13"
# PIN_TO_READ = "a0"

# How often to request a reading (in seconds)
READ_INTERVAL = 2

# --- MQTT Topics ---
# Topic to publish read requests to
REQUEST_TOPIC = f"br/{PIN_TO_READ[0]}/{PIN_TO_READ[1:]}/read"

# Topic to subscribe to for receiving the pin's value
VALUE_TOPIC = f"br/{PIN_TO_READ[0]}/{PIN_TO_READ[1:]}/value"


async def main() -> None:
    """Run main test logic."""
    logging.info(
        "Requesting a reading from pin %s every %d seconds.", PIN_TO_READ, READ_INTERVAL
    )
    logging.info("Press Ctrl+C to exit.")

    try:
        async with get_mqtt_client() as client:
            await client.subscribe(VALUE_TOPIC)
            logging.info("Subscribed to topic: %s", VALUE_TOPIC)

            # Start a task to listen for messages
            message_handler_task = asyncio.create_task(handle_messages(client))

            # Loop to send read requests
            while True:
                logging.info("Sending read request to %s", REQUEST_TOPIC)
                await client.publish(REQUEST_TOPIC, "read")
                await asyncio.sleep(READ_INTERVAL)

    except asyncio.CancelledError:
        logging.info("\nExiting...")
    except Exception as e:
        logging.error("An error occurred: %s", e)


async def handle_messages(client: aiomqtt.Client) -> None:
    """An async task to handle incoming MQTT messages."""
    async for message in client.messages:
        logging.info(
            "Received value for pin %s: %s",
            PIN_TO_READ,
            message.payload.decode(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
