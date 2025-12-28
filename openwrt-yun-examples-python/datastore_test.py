#!/usr/bin/env python3
"""Exercise datastore interactions using the bridge client."""

import asyncio
import logging
import argparse

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def main() -> None:
    """Run main test logic."""
    parser = argparse.ArgumentParser(description="DataStore feature test.")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    args = parser.parse_args()

    dump_client_env(logging.getLogger(__name__))

    bridge_args = {}
    if args.host:
        bridge_args["host"] = args.host
    if args.port:
        bridge_args["port"] = args.port
    if args.user:
        bridge_args["username"] = args.user
    if args.password:
        bridge_args["password"] = args.password

    bridge = Bridge(**bridge_args)
    await bridge.connect()

    try:
        logging.info("--- Starting DataStore Bridge Client Test ---")

        # --- Test 1: Put and Get a new key-value pair ---
        logging.info("[Test 1: Put and Get a new key-value pair]")
        key1: str = "client_test/temperature"
        value1: str = "25.5"

        await bridge.put(key1, value1)
        logging.info(f"Put value '{value1}' to key '{key1}'")

        retrieved_value: str = await bridge.get(key1)
        if retrieved_value == value1:
            logging.info(
                "SUCCESS: Retrieved value '%s' matches put value '%s'.",
                retrieved_value,
                value1,
            )
        else:
            logging.error(
                "FAILURE: Retrieved value '%s' does not match put value '%s'.",
                retrieved_value,
                value1,
            )

        # --- Test 2: Get a non-existent key ---
        logging.info("\n[Test 2: Get a non-existent key]")
        key2: str = "non_existent/key"

        retrieved_value_2: str = await bridge.get(key2)
        # Expecting an empty payload for a non-existent key
        if retrieved_value_2 == "":
            logging.info(
                "SUCCESS: Empty value returned for non-existent key '%s'.",
                key2,
            )
        else:
            logging.error(
                "FAILURE: Value '%s' returned for non-existent key '%s'.",
                retrieved_value_2,
                key2,
            )

        logging.info("\n--- Test Complete ---")

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
