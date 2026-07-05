#!/usr/bin/env python3
"""Poll sensor values via the async bridge client."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
    pin: str,
    interval: float,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as bridge:
        logging.info(
            "Requesting a reading from pin %s every %.1f seconds.",
            pin,
            interval,
        )
        logging.info("Press Ctrl+C to exit.")

        is_analog = pin.lower().startswith("a")
        # Handle optional 'd' or 'a' prefix safely
        try:
            raw_pin_str = pin[1:] if pin[0].isalpha() else pin
            pin_number = int(raw_pin_str)
        except ValueError:
            logging.error(f"Invalid pin format: {pin}")
            raise SystemExit(1)

        start_time = asyncio.get_running_loop().time()
        while True:
            if asyncio.get_running_loop().time() - start_time > 20.0:
                logging.info("Test duration of 20 seconds exceeded. Finishing.")
                break

            if is_analog:
                value: int = await bridge.analog_read(pin_number)
                logging.info(
                    "Received analog value for pin %s: %d",
                    pin,
                    value,
                )
            else:
                value = await bridge.digital_read(pin_number)
                logging.info(
                    "Received digital value for pin %s: %d",
                    pin,
                    value,
                )

            await asyncio.sleep(interval)


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
    pin: str = "d13",
    interval: float = 2.0,
) -> None:
    try:
        asyncio.run(run_test(socket_path, topic_prefix, pin, interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll sensor values via the async bridge client.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    parser.add_argument("--pin", default="d13", help="Pin to read (e.g., 'd13' or 'a0').")
    parser.add_argument("--interval", type=float, default=2.0, help="Read interval in seconds.")
    _args = parser.parse_args()
    main(
        _args.socket_path,
        _args.topic_prefix,
        _args.pin,
        _args.interval,
    )
