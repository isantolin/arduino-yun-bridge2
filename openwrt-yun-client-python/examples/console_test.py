"""Interactive console to send and receive messages from the Arduino console."""
import asyncio
import logging

import aiomqtt

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# MQTT Topics
TOPIC_CONSOLE_IN = "br/console/in"
TOPIC_CONSOLE_OUT = "br/console/out"


async def handle_incoming_messages(client: aiomqtt.Client):
    """Task to listen for messages from the Arduino console."""
    async for message in client.messages:
        logging.info("Received from Arduino: %s", message.payload.decode())


async def main() -> None:
    """Run main test logic."""
    logging.info("Connecting to MQTT broker...")
    logging.info("Enter text to send to the Arduino console. Type 'exit' to quit.")

    try:
        async with get_mqtt_client() as client:
            await client.subscribe(TOPIC_CONSOLE_OUT)
            logging.info("Subscribed to topic: %s", TOPIC_CONSOLE_OUT)

            # Start the message listening task
            listener_task = asyncio.create_task(handle_incoming_messages(client))

            while True:
                try:
                    # Run blocking input in a separate thread
                    message = await asyncio.to_thread(input)
                    if message.lower() == "exit":
                        break
                    await client.publish(TOPIC_CONSOLE_IN, message)
                except EOFError:
                    break
            
            # Clean up the listener task
            listener_task.cancel()
            await asyncio.gather(listener_task, return_exceptions=True)

    except asyncio.CancelledError:
        logging.info("\nExiting...")
    except Exception as e:
        logging.error("An error occurred: %s", e)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
