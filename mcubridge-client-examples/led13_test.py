#!/usr/bin/env python3
"""Example: Test generic pin control using the async McuBridge client."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    pin: int,
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as bridge:
        logging.info("--- Starting LED Pin Control Test ---")

        logging.info(f"Turning pin {pin} ON")
        await bridge.digital_write(pin, 1)
        await asyncio.sleep(2)

        logging.info(f"Turning pin {pin} OFF")
        await bridge.digital_write(pin, 0)
        await asyncio.sleep(2)

    logging.info("--- LED Test Complete ---")
    logging.info("Done.")


def main(
    pin: int = 13,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    tls_insecure: bool = False,
) -> None:
    asyncio.run(run_test(pin, host, port, user, password, tls_insecure))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test generic pin control using the async McuBridge client."
    )
    parser.add_argument("pin", type=int, nargs="?", default=13, help="Pin number")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        default=False,
        help="Disable TLS certificate verification",
    )
    _args = parser.parse_args()
    main(
        _args.pin,
        _args.host,
        _args.port,
        _args.user,
        _args.password,
        _args.tls_insecure,
    )
