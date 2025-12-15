#!/usr/bin/env python3
"""Example: Test generic pin control using the async YunBridge client."""

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
    parser = argparse.ArgumentParser(description="Test generic pin control.")
    parser.add_argument("pin", type=int, nargs="?", default=13, help="Pin number (default: 13)")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    args = parser.parse_args()

    dump_client_env(logging.getLogger(__name__))

    # Build arguments dict, only including provided values to allow Bridge defaults (env vars) to work
    bridge_args = {}
    if args.host:
        bridge_args["host"] = args.host
    if args.port:
        bridge_args["port"] = args.port
    if args.user:
        bridge_args["username"] = args.user
    if args.password:
        bridge_args["password"] = args.password

    # Option 2: Pass credentials to the constructor
    # If None, the library will try to use environment variables
    bridge = Bridge(**bridge_args)

    await bridge.connect()

    pin: int = args.pin

    try:
        logging.info(f"Turning pin {pin} ON")
        await bridge.digital_write(pin, 1)
        await asyncio.sleep(2)

        logging.info(f"Turning pin {pin} OFF")
        await bridge.digital_write(pin, 0)
        await asyncio.sleep(2)

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
