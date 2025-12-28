#!/usr/bin/env python3
"""Poll sensor values via the async bridge client."""

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
    parser = argparse.ArgumentParser(description="Sensor reader test.")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument(
        "--pin", 
        default="d13", 
        help="Pin to read (e.g., 'd13' or 'a0'). Default: d13"
    )
    parser.add_argument(
        "--interval", 
        type=float, 
        default=2.0, 
        help="Read interval in seconds. Default: 2.0"
    )
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

    pin_to_read = args.pin
    read_interval = args.interval

    logging.info(
        "Requesting a reading from pin %s every %.1f seconds.",
        pin_to_read,
        read_interval,
    )
    logging.info("Press Ctrl+C to exit.")

    is_analog = pin_to_read.lower().startswith("a")
    # Handle optional 'd' or 'a' prefix safely
    try:
        raw_pin_str = pin_to_read[1:] if pin_to_read[0].isalpha() else pin_to_read
        pin_number = int(raw_pin_str)
    except ValueError:
        logging.error(f"Invalid pin format: {pin_to_read}")
        return

    try:
        while True:
            if is_analog:
                value: int = await bridge.analog_read(pin_number)
                logging.info(
                    "Received analog value for pin %s: %d",
                    pin_to_read,
                    value,
                )
            else:
                value = await bridge.digital_read(pin_number)
                logging.info(
                    "Received digital value for pin %s: %d",
                    pin_to_read,
                    value,
                )

            await asyncio.sleep(read_interval)

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
