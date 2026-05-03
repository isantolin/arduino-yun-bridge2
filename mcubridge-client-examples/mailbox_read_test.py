#!/usr/bin/env python3
"""Example: Send a mailbox message and read back any MCU-forwarded responses."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = logging.getLogger(__name__)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
    max_polls: int,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as bridge:
        logger.info("--- Starting Mailbox Read Test ---")

        # --- Send phase ---
        message_to_send = "hello_from_mailbox_test"
        logger.info("Sending message to mailbox: '%s'", message_to_send)
        await bridge.mailbox_write(message_to_send)
        logger.info("Message sent successfully.")

        # --- Read phase ---
        logger.info("Polling for mailbox responses (max_polls=%d)...", max_polls)
        polls = 0
        while max_polls <= 0 or polls < max_polls:
            message: bytes | None = await bridge.mailbox_read(timeout=3)
            polls += 1
            if message is None:
                logger.info("No mailbox message within timeout; poll %d done.", polls)
                continue

            preview = message.decode("utf-8", errors="ignore")
            logger.info(
                "Received mailbox message (%d bytes): %s",
                len(message),
                preview,
            )
        if max_polls > 0:
            logger.info("Reached max polls (%d), exiting.", max_polls)

    logger.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    tls_insecure: bool = False,
    max_polls: int = 1,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure, max_polls))
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send a mailbox message and read back any MCU-forwarded responses."
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
    parser.add_argument(
        "--max-polls", type=int, default=1, help="Max poll cycles (0 = unlimited)"
    )
    _args = parser.parse_args()
    main(
        _args.host,
        _args.port,
        _args.user,
        _args.password,
        _args.tls_insecure,
        _args.max_polls,
    )
