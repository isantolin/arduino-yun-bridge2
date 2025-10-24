#!/usr/bin/env python3
"""Example: Test generic pin control using the async YunBridge client.

Sends messages to control and monitor any pin state (default: 13)

Usage:
    python3 led13_test.py [PIN]
"""
import asyncio
import logging
import sys

from example_utils import get_mqtt_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def main() -> None:
    """Run main test logic."""
    pin = "13"
    if len(sys.argv) > 1:
        pin = sys.argv[1]

    pin_function_short = "a" if pin.upper().startswith("A") else "d"
    topic_set = f"br/{pin_function_short}/{pin}"

    try:
        async with get_mqtt_client() as client:
            logging.info("Turning pin %s ON via MQTT...", pin)
            await client.publish(topic_set, "1")
            await asyncio.sleep(2)

            logging.info("Turning pin %s OFF via MQTT...", pin)
            await client.publish(topic_set, "0")
            await asyncio.sleep(2)
    except Exception as e:
        logging.error("An error occurred: %s", e)

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
