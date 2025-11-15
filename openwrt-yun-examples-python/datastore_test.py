"""Example: Test the DataStore functionality of the Yun Bridge """
""" using the Bridge client."""
import asyncio
import logging

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s - "
                                               "%(levelname)s - "
                                               "%(message)s")


async def main() -> None:
    """Run main test logic."""
    dump_client_env(logging.getLogger(__name__))
    bridge = Bridge()
    await bridge.connect()

    try:
        logging.info("--- Starting DataStore Bridge Client Test ---")

        # --- Test 1: Put and Get a new key-value pair ---
        logging.info("[Test 1: Put and Get a new key-value pair]")
        key1 = "client_test/temperature"
        value1 = "25.5"

        await bridge.put(key1, value1)
        logging.info(f"Put value '{value1}' to key '{key1}'")

        retrieved_value = await bridge.get(key1)
        if retrieved_value == value1:
            logging.info(f"SUCCESS: Retrieved value '{retrieved_value}' matches put value '{value1}'.")
        else:
            logging.error(f"FAILURE: Retrieved value '{retrieved_value}' does not match put value '{value1}'.")

        # --- Test 2: Get a non-existent key ---
        logging.info("\n[Test 2: Get a non-existent key]")
        key2 = "non_existent/key"
        
        retrieved_value_2 = await bridge.get(key2)
        # Expecting an empty payload for a non-existent key
        if retrieved_value_2 is None or retrieved_value_2 == "":
            logging.info(f"SUCCESS: Correctly received empty value for non-existent key '{key2}'.")
        else:
            logging.error(f"FAILURE: Incorrectly received value '{retrieved_value_2}' for non-existent key.")

        logging.info("\n--- Test Complete ---")

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
