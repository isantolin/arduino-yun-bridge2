#!/usr/bin/env python3
"""Exercise datastore interactions using the bridge client."""

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
        description="Exercise datastore interactions using the bridge client."
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
