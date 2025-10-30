"""Interactive console to send and receive messages from the Arduino console."""
import asyncio
import logging

from yunbridge_client import Bridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def main() -> None:
    """Run main test logic."""
    bridge = Bridge()
    await bridge.connect()

    logging.info("Enter text to send to the Arduino console. Type 'exit' to quit.")

    try:
        # Start a task to listen for console messages
        async def console_listener():
            while True:
                message = await bridge.console_read_async()
                if message:
                    logging.info(f"Received from Arduino: {message}")
                else:
                    await asyncio.sleep(0.1)
        
        listener_task = asyncio.create_task(console_listener())

        while True:
            try:
                # Run blocking input in a separate thread
                message = await asyncio.to_thread(input)
                if message.lower() == "exit":
                    break
                await bridge.console_write(message)
            except EOFError:
                break
        
        # Clean up the listener task
        listener_task.cancel()
        await asyncio.gather(listener_task, return_exceptions=True)

    except asyncio.CancelledError:
        logging.info("\nExiting...")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
