"""Interactive console helper for the Arduino bridge."""
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

    logging.info(
        "Enter text to send to the Arduino console. Type 'exit' to quit."
    )

    try:
        # Start a task to listen for console messages
        async def console_listener() -> None:
            while True:
                message = await bridge.console_read_async()
                if message is not None:
                    logging.info("Received from Arduino: %s", message)
                else:
                    await asyncio.sleep(0.1)

        listener_task: asyncio.Task[None] = asyncio.create_task(
            console_listener()
        )

        while True:
            try:
                # Run blocking input in a separate thread
                user_input = await asyncio.to_thread(input)
                if user_input.lower() == "exit":
                    break
                await bridge.console_write(user_input)
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
