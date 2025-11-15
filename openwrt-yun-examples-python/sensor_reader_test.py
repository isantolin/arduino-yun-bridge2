"""Example: Periodically read a sensor value from a pin using an async client."""
import asyncio
import logging

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Configuration ---
# The pin to read. Use a format like 'd13' for digital or 'a0' for analog.
PIN_TO_READ = "d13"
# PIN_TO_READ = "a0"

# How often to request a reading (in seconds)
READ_INTERVAL = 2


async def main() -> None:
    """Run main test logic."""
    dump_client_env(logging.getLogger(__name__))
    bridge = Bridge()
    await bridge.connect()

    logging.info(f"Requesting a reading from pin {PIN_TO_READ} "
                 "every {READ_INTERVAL} seconds.")
    logging.info("Press Ctrl+C to exit.")

    is_analog = PIN_TO_READ.lower().startswith('a')
    pin_number = int(PIN_TO_READ[1:])

    try:
        while True:
            if is_analog:
                value = await bridge.analog_read(pin_number)
                logging.info(f"Received analog value for pin {PIN_TO_READ}: {value}")
            else:
                value = await bridge.digital_read(pin_number)
                logging.info(f"Received digital value for pin {PIN_TO_READ}: {value}")
            
            await asyncio.sleep(READ_INTERVAL)

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
