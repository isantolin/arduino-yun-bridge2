#!/usr/bin/env python3
"""Example: Test file I/O using the async McuBridge client."""

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
    tls_insecure: bool,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as bridge:
        test_filename: str = "/tmp/test_file.txt"
        test_content: str = "hello from async fileio_test"

        try:
            # --- Test File Write ---
            logging.info(f"Writing '{test_content}' to {test_filename}")
            await bridge.file_write(test_filename, test_content)

            # --- Test File Read ---
            logging.info(f"Reading from {test_filename}")
            content: bytes = await bridge.file_read(test_filename)
            decoded = content.decode()
            logging.info("Read content: %s", decoded)

            if decoded == test_content:
                logging.info("SUCCESS: Read content matches written content.")
            else:
                logging.error("FAILURE: Read content does not match written content.")

        finally:
            # --- Test File Remove ---
            logging.info("Removing %s", test_filename)
            await bridge.file_remove(test_filename)

    logging.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    tls_insecure: bool = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test file I/O using the async McuBridge client."
    )
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
    main(_args.host, _args.port, _args.user, _args.password, _args.tls_insecure)
