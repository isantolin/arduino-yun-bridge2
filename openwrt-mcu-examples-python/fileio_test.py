#!/usr/bin/env python3
"""Example: Test file I/O using the async McuBridge client."""

import asyncio
import logging
import argparse
import ssl

from mcubridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def main() -> None:
    """Run main test logic."""
    parser = argparse.ArgumentParser(description="File I/O feature test.")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument("--tls-insecure", action="store_true", help="Disable TLS certificate verification")
    args = parser.parse_args()

    # Validate essential arguments if not running on OpenWrt with UCI
    if not args.host or not args.user or not args.password:
        from mcubridge_client.env import read_uci_general
        if not read_uci_general():
            logging.info("Error: Missing required connection parameters.")
            parser.print_help()
            return

    dump_client_env(logging.getLogger(__name__))

    bridge_args: dict[str, object] = {}
    if args.host:
        bridge_args["host"] = args.host
    if args.port:
        bridge_args["port"] = args.port
    if args.user:
        bridge_args["username"] = args.user
    if args.password:
        bridge_args["password"] = args.password
    if args.tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        bridge_args["tls_context"] = ctx

    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

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
            logging.error("FAILURE: Read content does not match written " "content.")

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        # --- Test File Remove ---
        logging.info("Removing %s", test_filename)
        await bridge.file_remove(test_filename)
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
