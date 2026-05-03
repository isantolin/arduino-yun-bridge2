#!/usr/bin/env python3
"""Poll sensor values via the async bridge client."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    pin: str,
    interval: float,
    tls_insecure: bool,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as bridge:
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
            if asyncio.get_running_loop().time() - start_time > 10.0:
                logging.info("Test duration of 10 seconds exceeded. Finishing.")
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
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    pin: str = "d13",
    interval: float = 2.0,
    tls_insecure: bool = False,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, pin, interval, tls_insecure))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Poll sensor values via the async bridge client."
    )
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument(
        "--pin", default="d13", help="Pin to read (e.g., 'd13' or 'a0')."
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="Read interval in seconds."
    )
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        default=False,
        help="Disable TLS certificate verification",
    )
    _args = parser.parse_args()
    main(
        _args.host,
        _args.port,
        _args.user,
        _args.password,
        _args.pin,
        _args.interval,
        _args.tls_insecure,
    )
