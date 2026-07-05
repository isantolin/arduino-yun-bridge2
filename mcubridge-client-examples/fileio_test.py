#!/usr/bin/env python3
"""Example: Test file I/O using the async McuBridge client."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as bridge:
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
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test file I/O using the async McuBridge client.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)
